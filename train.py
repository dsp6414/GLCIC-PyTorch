from tqdm import tqdm
from models import CompletionNetwork, ContextDiscriminator
from datasets import ImageDataset
from losses import completion_network_loss
from utils import (
    gen_input_mask,
    gen_hole_area,
    crop,
    sample_random_batch,
    poisson_blend,
)
from torch.utils.data import DataLoader
from torch.optim import Adadelta
from torch.nn import BCELoss
from torchvision.utils import save_image
from PIL import Image
import torchvision.transforms as transforms
import torch
import random
import os
import argparse
import numpy as np
import json


parser = argparse.ArgumentParser()
parser.add_argument('data_dir')
parser.add_argument('result_dir')
parser.add_argument('--Tc', type=int, default=90000)
parser.add_argument('--Td', type=int, default=10000)
parser.add_argument('--Ttrain', type=int, default=500000)
parser.add_argument('--snaperiod_1', type=int, default=18000)
parser.add_argument('--snaperiod_2', type=int, default=2000)
parser.add_argument('--snaperiod_3', type=int, default=80000)
parser.add_argument('--max_patches', type=int, default=1)
parser.add_argument('--ptch_reg_w', type=int, default=96)
parser.add_argument('--ptch_reg_h', type=int, default=96)
parser.add_argument('--ptch_min_w', type=int, default=48)
parser.add_argument('--ptch_max_w', type=int, default=96)
parser.add_argument('--ptch_min_h', type=int, default=48)
parser.add_argument('--ptch_max_h', type=int, default=96)
parser.add_argument('--cn_input_size', type=int, default=160)
parser.add_argument('--gd_input_size', type=int, default=160)
parser.add_argument('--ld_input_size', type=int, default=96)
parser.add_argument('--bsize', type=int, default=16)
parser.add_argument('--shuffle', default=True)
parser.add_argument('--no_cuda', default=False)
parser.add_argument('--lr_cn', type=float, default=1.0)
parser.add_argument('--rho_cn', type=float, default=0.9)
parser.add_argument('--wd_cn', type=float, default=0.0)
parser.add_argument('--lr_cd', type=float, default=1.0)
parser.add_argument('--rho_cd', type=float, default=0.9)
parser.add_argument('--wd_cd', type=float, default=0.0)
parser.add_argument('--alpha', type=float, default=4e-4)
parser.add_argument('--comp_mpv', default=True)


def main(args):

    # ================================================
    # Preparation
    # ================================================
    args.data_dir = os.path.expanduser(args.data_dir)
    args.result_dir = os.path.expanduser(args.result_dir)

    use_cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')

    # create result directory (if necessary)
    if os.path.exists(args.result_dir) == False:
        os.makedirs(args.result_dir)
    for s in ['phase_1', 'phase_2', 'phase_3']:
        if os.path.exists(os.path.join(args.result_dir, s)) == False:
            os.makedirs(os.path.join(args.result_dir, s))

    # dataset
    trnsfm = transforms.Compose([
        transforms.Resize(args.cn_input_size),
        transforms.RandomCrop((args.cn_input_size, args.cn_input_size)),
        transforms.ToTensor(),
    ])
    print('loading dataset...')
    train_dset = ImageDataset(os.path.join(args.data_dir, 'train'), trnsfm)
    test_dset = ImageDataset(os.path.join(args.data_dir, 'test'), trnsfm)
    train_loader = DataLoader(train_dset, batch_size=args.bsize, shuffle=args.shuffle)

    # compute the mean pixel value of datasets
    mean_pv = 0.
    if args.comp_mpv:
        imgpaths = train_dset.imgpaths + test_dset.imgpaths
        pbar = tqdm(total=len(imgpaths), desc='computing the mean pixel value')
        for imgpath in imgpaths:
            img = Image.open(imgpath)
            x = np.array(img, dtype=np.float32)
            mean_pv += x.mean()
            pbar.update()
        pbar.close()
        mean_pv /= len(imgpaths)
        mean_pv /= 255. # normalize
    mpv = torch.tensor(mean_pv).to(device)

    # save training config
    args_dict = vars(args)
    args_dict['mean_pv'] = mean_pv
    with open(os.path.join(args.result_dir, 'config.json'), mode='w') as f:
        json.dump(args_dict, f)


    # ================================================
    # Training Phase 1
    # ================================================
    # model & optimizer
    model_cn = CompletionNetwork()
    model_cn = model_cn.to(device)
    opt_cn = Adadelta(model_cn.parameters(), lr=args.lr_cn, rho=args.rho_cn, weight_decay=args.wd_cn)

    # training
    pbar = tqdm(total=args.Tc)
    while pbar.n < args.Tc:
        for x in train_loader:

            opt_cn.zero_grad()

            # generate patch region
            ptch_reg = gen_hole_area(
                size=(args.ptch_reg_w, args.ptch_reg_h),
                mask_size=(x.shape[3], x.shape[2]),
            )

            # create mask
            msk = gen_input_mask(
                shape=x.shape,
                hole_size=(
                    (args.ptch_min_w, args.ptch_max_w),
                    (args.ptch_min_h, args.ptch_max_h)),
                ),
                hole_area=ptch_reg,
                max_holes=args.max_patches,
            )

            # merge x, mask, and mpv
            msg = 'phase 1 |'
            x = x.to(device)
            msk = msk.to(device)
            input = x - x * msk + mpv * msk
            output = model_cn(input)
            loss = completion_network_loss(x, output, msk)
            loss.backward()
            opt_cn.step()

            msg += ' train loss: %.5f' % loss.cpu()
            pbar.set_description(msg)
            pbar.update()

            # test
            if pbar.n % args.snaperiod_1 == 0:
                with torch.no_grad():

                    x = sample_random_batch(test_dset, batch_size=args.bsize)
                    x = x.to(device)
                    input = x - x * msk + mpv * msk
                    output = model_cn(input)
                    completed = poisson_blend(input, output, msk)
                    imgs = torch.cat((input.cpu(), completed.cpu()), dim=0)
                    save_image(imgs, os.path.join(args.result_dir, 'phase_1', 'step%d.png' % pbar.n), nrow=len(x))
                    torch.save(model_cn.state_dict(), os.path.join(args.result_dir, 'phase_1', 'model_cn_step%d' % pbar.n))

            if pbar.n >= args.Tc:
                break
    pbar.close()


    # ================================================
    # Training Phase 2
    # ================================================
    # model, optimizer & criterion
    model_cd = ContextDiscriminator(
        local_input_shape=(3, args.ld_input_size, args.ld_input_size),
        global_input_shape=(3, args.gd_input_size, args.gd_input_size),
    )
    model_cd = model_cd.to(device)
    opt_cd = Adadelta(model_cd.parameters(), lr=args.lr_cd, rho=args.rho_cd, weight_decay=args.wd_cd)
    criterion_cd = BCELoss()

    # training
    pbar = tqdm(total=args.Td)
    while pbar.n < args.Td:
        for x in train_loader:

            opt_cd.zero_grad()
            x = x.to(device)

            # ================================================
            # fake
            # ================================================
            ptch_reg = gen_hole_area(
                size=(args.ptch_reg_w, args.ptch_reg_h),
                mask_size=(x.shape[3], x.shape[2]),
            )

            # create mask
            msk = gen_input_mask(
                shape=x.shape,
                hole_size=(
                    (args.ptch_min_w, args.ptch_max_w),
                    (args.ptch_min_h, args.ptch_max_h)),
                ),
                hole_area=ptch_reg,
                max_holes=args.max_patches,
            )

            fake = torch.zeros((len(x), 1)).to(device)
            msk = msk.to(device)
            input_cn = x - x * msk + mpv * msk
            output_cn = model_cn(input_cn)
            input_gd_fake = output_cn.detach()
            input_ld_fake = crop_patch_region(input_gd_fake, ptch_reg)
            input_fake = (input_ld_fake, input_gd_fake)
            output_fake = model_cd(input_fake)
            loss_fake = criterion_cd(output_fake, fake)

            # ================================================
            # real
            # ================================================
            ptch_reg = gen_hole_area(
                size=(args.ptch_reg_w, args.ptch_reg_h),
                mask_size=(x.shape[3], x.shape[2]),
            )

            real = torch.ones((len(x), 1)).to(device)
            input_gd_real = x
            input_ld_real = crop_patch_region(input_gd_real, ptch_reg)
            input_real = (input_ld_real, input_gd_real)
            output_real = model_cd(input_real)
            loss_real = criterion_cd(output_real, real)

            # ================================================
            # optimize
            # ================================================
            loss = (loss_fake + loss_real) / 2.
            loss.backward()
            opt_cd.step()

            msg = 'phase 2 |'
            msg += ' train loss: %.5f' % loss.cpu()
            pbar.set_description(msg)
            pbar.update()

            # test
            if pbar.n % args.snaperiod_2 == 0:
                with torch.no_grad():

                    x = sample_random_batch(test_dset, batch_size=args.bsize)
                    x = x.to(device)
                    input = x - x * msk + mpv * msk
                    output = model_cn(input)
                    completed = poisson_blend(input, output, msk)
                    imgs = torch.cat((input.cpu(), completed.cpu()), dim=0)
                    save_image(imgs, os.path.join(args.result_dir, 'phase_2', 'step%d.png' % pbar.n), nrow=len(x))
                    torch.save(model_cd.state_dict(), os.path.join(args.result_dir, 'phase_2', 'model_cd_step%d' % pbar.n))

            if pbar.n >= args.Td:
                break
    pbar.close()


    # ================================================
    # Training Phase 3
    # ================================================
    # training
    n_steps = args.Ttrain - (args.Tc + args.Td)
    alpha = torch.tensor(args.alpha).to(device)
    pbar = tqdm(total=n_steps)
    while pbar.n < n_steps:
        for x in train_loader:

            x = x.to(device)

            # ================================================
            # train model_cd
            # ================================================
            opt_cd.zero_grad()

            # fake
            ptch_reg = gen_hole_area(
                size=(args.ptch_reg_w, args.ptch_reg_h),
                mask_size=(x.shape[3], x.shape[2]),
            )

            # create mask
            msk = gen_input_mask(
                shape=x.shape,
                hole_size=(
                    (args.ptch_min_w, args.ptch_max_w),
                    (args.ptch_min_h, args.ptch_max_h)),
                ),
                hole_area=ptch_reg,
                max_holes=args.max_patches,
            )

            fake = torch.zeros((len(x), 1)).to(device)
            msk = msk.to(device)
            input_cn = x - x * msk + mpv * msk
            output_cn = model_cn(input_cn)
            input_gd_fake = output_cn.detach()
            input_ld_fake = crop_patch_region(input_gd_fake, ptch_reg)
            input_fake = (input_ld_fake, input_gd_fake)
            output_fake = model_cd(input_fake)
            loss_cd_1 = criterion_cd(output_fake, fake)

            # real
            ptch_reg = gen_hole_area(
                size=(args.ptch_reg_w, args.ptch_reg_h),
                mask_size=(x.shape[3], x.shape[2]),
            )

            real = torch.ones((len(x), 1)).to(device)
            input_gd_real = x
            input_ld_real = crop_patch_region(input_gd_real, ptch_reg)
            input_real = (input_ld_real, input_gd_real)
            output_real = model_cd(input_real)
            loss_cd_2 = criterion_cd(output_real, real)

            # optimize
            loss_cd = (loss_cd_1 + loss_cd_2) * alpha / 2.
            loss_cd.backward()
            opt_cd.step()

            # ================================================
            # train model_cn
            # ================================================
            opt_cn.zero_grad()

            loss_cn_1 = completion_network_loss(x, output_cn, msk)
            input_gd_fake = output_cn
            input_ld_fake = crop_patch_region(input_gd_fake, ptch_reg)
            input_fake = (input_ld_fake, input_gd_fake)
            output_fake = model_cd(input_fake)
            loss_cn_2 = criterion_cd(output_fake, real)

            # optimize
            loss_cn = (loss_cn_1 + alpha * loss_cn_2) / 2.
            loss_cn.backward()
            opt_cn.step()

            msg = 'phase 3 |'
            msg += ' train loss (cd): %.5f' % loss_cd.cpu()
            msg += ' train loss (cn): %.5f' % loss_cn.cpu()
            pbar.set_description(msg)
            pbar.update()

            # test
            if pbar.n % args.snaperiod_3 == 0:
                with torch.no_grad():

                    x = sample_random_batch(test_dset, batch_size=args.bsize)
                    x = x.to(device)
                    input = x - x * msk + mpv * msk
                    output = model_cn(input)
                    completed = poisson_blend(input, output, msk)
                    imgs = torch.cat((input.cpu(), completed.cpu()), dim=0)
                    save_image(imgs, os.path.join(args.result_dir, 'phase_3', 'step%d.png' % pbar.n), nrow=len(x))
                    torch.save(model_cn.state_dict(), os.path.join(args.result_dir, 'phase_3', 'model_cn_step%d' % pbar.n))
                    torch.save(model_cd.state_dict(), os.path.join(args.result_dir, 'phase_3', 'model_cd_step%d' % pbar.n))

            if pbar.n >= n_steps:
                break
    pbar.close()


if __name__ == '__main__':
    args = parser.parse_args()
    main(args)
