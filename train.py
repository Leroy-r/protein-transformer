'''
This script handling the training process.
'''

import argparse
import math
import time

import numpy as np
from tqdm import tqdm
import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data
import transformer.Constants as Constants
from dataset import paired_collate_fn, ProteinDataset
from transformer.Models import Transformer
from transformer.Optim import ScheduledOptim
from transformer.Structure import angles2coords, drmsd
from torch import multiprocessing


def unpad_angle_vectors(pred, gold, device):
    not_padded_mask = (gold != 0)
    if device.type == "cuda":
        pred_unpadded = pred.cuda() * not_padded_mask.type(torch.cuda.FloatTensor)
        gold_unpadded = gold.cuda() * not_padded_mask.type(torch.cuda.FloatTensor)
    else:
        pred_unpadded = pred * not_padded_mask.type(torch.FloatTensor)
        gold_unpadded = gold * not_padded_mask.type(torch.FloatTensor)
    return pred_unpadded, gold_unpadded


def inverse_trig_transform(t):
    """ Given a (BATCH x L X 22) tensor, returns (BATCH X L X 11) tensor.
        Performs atan2 transformation from sin and cos values."""
    t = t.view(t.shape[0], -1, 11, 2)
    t_cos = t[:, :, :, 0]
    t_sin = t[:, :, :, 1]
    t = torch.atan2(t_sin, t_cos)
    return t


def cal_loss(pred, gold, device):
    ''' Calculate DRMSD loss. '''
    device = torch.device("cpu")
    pred, gold = pred.to(device), gold.to(device)

    pred, gold = inverse_trig_transform(pred), inverse_trig_transform(gold)
    pred, gold = unpad_angle_vectors(pred, gold, device)

    losses = []
    for pred_item, gold_item in zip(pred, gold):
        true_coords = angles2coords(gold_item, device)
        pred_coords = angles2coords(pred_item, device)
        loss = drmsd(pred_coords, true_coords)
        losses.append(loss)

    return torch.mean(torch.stack(losses))


def mse_loss(pred, gold):
    """ Computes MSE loss."""
    device = torch.device("cpu")
    pred, gold = pred.to(device), gold.to(device)
    pred_unpadded, gold_unpadded = unpad_angle_vectors(pred, gold, device)
    return F.mse_loss(pred_unpadded, gold_unpadded)

def train_epoch(model, training_data, optimizer, device):
    ''' Epoch operation in training phase'''

    model.train()

    total_loss = 0
    n_batches = 0.0
    loss = None
    training_losses = []
    pbar = tqdm(training_data, mininterval=2, desc='  - (Training) Loss = {0}   '.format(loss), leave=False)

    for batch_num, batch in enumerate(pbar):

        # prepare data
        src_seq, src_pos, tgt_seq, tgt_pos = map(lambda x: x.to(device), batch)
        gold = tgt_seq[:]

        # forward
        optimizer.zero_grad()
        pred = model(src_seq, src_pos, tgt_seq, tgt_pos)

        # backward
        loss = cal_loss(pred, gold, device)
        training_losses.append(float(loss))
        loss.backward()

        # update parameters
        optimizer.step_and_update_lr()
        # optimizer.step()

        # note keeping
        total_loss += loss.item()
        n_batches += 1

        pbar.set_description('  - (Training) Loss = {0:.6f}   '.format(float(loss)))
        if batch_num % 5 == 0 and len(training_losses) > 5:
            print("Last 32 avg loss = {0:.4f}".format(np.mean(training_losses[-32:])))

    return total_loss / n_batches

def eval_epoch(model, validation_data, device):
    ''' Epoch operation in evaluation phase '''

    model.eval()

    total_loss = 0
    n_batches = 0.0


    with torch.no_grad():
        for batch in validation_data:
            # tqdm(
            #     validation_data, mininterval=2,
            #     desc='  - (Validation) ', leave=False):

            # prepare data
            src_seq, src_pos, tgt_seq, tgt_pos = map(lambda x: x.to(device), batch)

            gold = tgt_seq[:]

            # forward
            pred = model(src_seq, src_pos, tgt_seq, tgt_pos)
            loss = cal_loss(pred, gold, device)

            # note keeping
            total_loss += loss.item()
            n_batches += 1

    return total_loss / n_batches

def train(model, training_data, validation_data, optimizer, device, opt):
    ''' Start training '''

    log_train_file = None
    log_valid_file = None

    # Set up training/validation log files.
    if opt.log:
        log_train_file = opt.log + '.train.log'
        log_valid_file = opt.log + '.valid.log'

        print('[Info] Training performance will be written to file: {} and {}'.format(
            log_train_file, log_valid_file))

        with open(log_train_file, 'w') as log_tf, open(log_valid_file, 'w') as log_vf:
            log_tf.write('epoch,loss\n')
            log_vf.write('epoch,loss\n')

    valid_losses = []
    epoch_last_improved = -1
    best_valid_loss_so_far = 10000
    for epoch_i in range(opt.epoch):
        print('[ Epoch', epoch_i, ']')

        start = time.time()
        train_loss = train_epoch(
            model, training_data, optimizer, device)
        print('  - (Training)   loss: {loss: 8.5f} '\
              'elapse: {elapse:3.3f} min'.format(
                  loss=train_loss,
                  elapse=(time.time()-start)/60))

        start = time.time()
        valid_loss = eval_epoch(model, validation_data, device)
        print('  - (Validation) loss: {loss: 8.5f}, '\
                'elapse: {elapse:3.3f} min'.format(
                    loss=valid_loss,
                    elapse=(time.time()-start)/60))

        valid_losses.append(valid_loss)

        if opt.step_when and valid_loss < best_valid_loss_so_far:
            best_valid_loss_so_far = valid_loss
            epoch_last_improved = epoch_i
        elif opt.step_when and epoch_i - epoch_last_improved > opt.step_when:
            # Model hasn't improved in 100 epochs
            print("No improvement for 100 epochs. Stopping model training early.")
            break


        # Record model state and log training info
        model_state_dict = model.state_dict()
        checkpoint = {
            'model': model_state_dict,
            'settings': opt,
            'epoch': epoch_i}

        if opt.save_model:
            if opt.save_mode == 'all':
                model_name = opt.save_model + '_loss_{vloss:3.3f}.chkpt'.format(vloss=valid_loss)
                torch.save(checkpoint, model_name)
            elif opt.save_mode == 'best':
                model_name = opt.save_model + '.chkpt'
                if valid_loss <= min(valid_losses):
                    torch.save(checkpoint, model_name)
                    print('    - [Info] The checkpoint file has been updated.')

        if log_train_file and log_valid_file:
            with open(log_train_file, 'a') as log_tf, open(log_valid_file, 'a') as log_vf:
                log_tf.write('{epoch},{loss: 8.5f}\n'.format(
                    epoch=epoch_i, loss=train_loss))
                log_vf.write('{epoch},{loss: 8.5f}\n'.format(
                    epoch=epoch_i, loss=valid_loss))

def main():
    ''' Main function '''
    parser = argparse.ArgumentParser()

    parser.add_argument('-data', required=True)

    parser.add_argument('-epoch', type=int, default=10)
    parser.add_argument('-batch_size', type=int, default=64)
    parser.add_argument('-step_when', type=int, default=None)

    parser.add_argument('-d_word_vec', type=int, default=20)
    parser.add_argument('-d_model', type=int, default=256)
    parser.add_argument('-d_inner_hid', type=int, default=1024)
    parser.add_argument('-d_k', type=int, default=64)
    parser.add_argument('-d_v', type=int, default=64)

    parser.add_argument('-n_head', type=int, default=8)
    parser.add_argument('-n_layers', type=int, default=6)
    parser.add_argument('-n_warmup_steps', type=int, default=10)

    parser.add_argument('-dropout', type=float, default=0.1)

    parser.add_argument('-log', default=None)
    parser.add_argument('-save_model', default=None)
    parser.add_argument('-save_mode', type=str, choices=['all', 'best'], default='best')

    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-label_smoothing', action='store_true')

    opt = parser.parse_args()
    opt.cuda = not opt.no_cuda
    opt.d_word_vec = opt.d_model

    #========= Loading Dataset =========#
    data = torch.load(opt.data)
    opt.max_token_seq_len = data['settings']["max_len"]

    training_data, validation_data = prepare_dataloaders(data, opt)

    #========= Preparing Model =========#

    print(opt)

    device = torch.device('cuda' if opt.cuda else 'cpu')
    transformer = Transformer(
        opt.max_token_seq_len,
        d_k=opt.d_k,
        d_v=opt.d_v,
        d_model=opt.d_model,
        d_inner=opt.d_inner_hid,
        n_layers=opt.n_layers,
        n_head=opt.n_head,
        dropout=opt.dropout).to(device)

    optimizer = ScheduledOptim(
        optim.Adam(
            filter(lambda x: x.requires_grad, transformer.parameters()),
            betas=(0.9, 0.98), eps=1e-09, lr=1e-4),
        opt.d_model, opt.n_warmup_steps)

    # optimizer =  optim.Adam(filter(lambda x: x.requires_grad, transformer.parameters()),
    #                         betas=(0.9, 0.98), eps=1e-09, lr=1e-3)

    train(transformer, training_data, validation_data, optimizer, device ,opt)

def prepare_dataloaders(data, opt):
    """ data is a dictionary containing all necessary training data."""
    # ========= Preparing DataLoader =========#
    # TODO create "data.pkl" file which is a dictionary with the necessary data
    train_loader = torch.utils.data.DataLoader(
        ProteinDataset(
            seqs=data['train']['seq'],
            angs=data['train']['ang']),
        num_workers=2,
        batch_size=opt.batch_size,
        collate_fn=paired_collate_fn,
        shuffle=True)

    valid_loader = torch.utils.data.DataLoader(
        ProteinDataset(
            seqs=data['valid']['seq'],
            angs=data['valid']['ang']),
        num_workers=2,
        batch_size=opt.batch_size,
        collate_fn=paired_collate_fn)
    return train_loader, valid_loader


if __name__ == '__main__':
    main()
