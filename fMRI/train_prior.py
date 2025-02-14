import os
import sys
import json
import random
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torchvision.utils import make_grid
from tqdm import tqdm
import pandas as pd
from collections import OrderedDict
from dalle2_pytorch import DiffusionPriorNetwork #, DiffusionPrior

import ddp_config
import utils
from models import Clipper, BrainNetwork, BrainDiffusionPrior, BrainSD

if __name__ == '__main__':
    # -----------------------------------------------------------------------------
    # params for this model
    model_name = "prior"
    modality = "image" # ("image", "text")
    clip_variant = "ViT-L/14" # ("RN50", "ViT-L/14", "ViT-B/32")
    clamp_embs = False # clamp embeddings to (-1.5, 1.5)
    # BrainNet checkpoint
    ckpt_path = f'checkpoints/clip_image_vitL_2stage_mixco_lotemp_125ep_subj01_best.pth'
    # timesteps = 1000
    timesteps = 100
    dim = 768
    depth = 6
    dim_head = 64
    heads = 12 # heads * dim_head = 12 * 64 = 768
    cond_drop_prob = 0.2
    image_embed_scale = None
    # image_embed_scale = 1.0
    condition_on_text_encodings = False
    clip_aug_mode = 'x' #  ('x', 'y', 'n')
    clip_aug_prob = 0.3
    # how many samples from train and val to save
    n_samples_save = 8
    save_samples_at_end = False
    # how many pairs of (orig, augmented) images to save
    n_aug_save = 16
    remote_data = False
    data_commit = '9947586218b6b7c8cab804009ddca5045249a38d'
    pretrained = False
    # -----------------------------------------------------------------------------
    # params for all models
    seed = 0
    batch_size = 128
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')    
    num_devices = torch.cuda.device_count()
    num_workers = num_devices
    num_epochs = 60
    lr_scheduler = 'cycle'
    initial_lr = 1e-3 #3e-5
    max_lr = 3e-4
    wandb_log = True
    wandb_project = 'laion-fmri'
    wandb_run_name = ''
    wandb_notes = ''
    first_batch = False
    ckpt_saving = True
    ckpt_interval = None
    outdir = os.path.expanduser(f'~/data/neuro/models/{model_name}/test')

    # -----------------------------------------------------------------------------
    config_keys = [k for k,v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
    exec(open('configurator.py').read()) # overrides from command line or config file
    config = {k: globals()[k] for k in config_keys} # will be useful for logging
    # -----------------------------------------------------------------------------
    
    print('config:')
    print(json.dumps(config, indent=2))

    utils.seed_everything(seed)

    assert clip_aug_mode in ('x', 'y', 'n')
    assert n_aug_save <= batch_size
    
    if modality == "text":
        image_var = "trial"
    elif modality == "image":
        image_var = "images"
    else:
        raise Exception(f"Unknown modality: {modality}")

    # setup multi-gpu Data Distributed Processing (ddp) if available
    # if not using ddp, using_ddp should be False and local_rank=0
    using_ddp, local_rank = ddp_config.ddp_test()
    if device == 'cuda':
        torch.cuda.set_device(local_rank)

    # write config
    # TODO: only on master process
    outdir = os.path.expanduser(outdir)
    os.makedirs(outdir, exist_ok=True)    
    with open(os.path.join(outdir, 'config.json'), 'w') as f:
        json.dump(config, f, indent=2)

    # load SD image variation pipeline
    sd_pipe = BrainSD.from_pretrained(
        "lambdalabs/sd-image-variations-diffusers", 
        revision="v2.0",
        safety_checker=None,
        requires_safety_checker=False,
    ).to(device)
    
    assert sd_pipe.image_encoder.training == False
    sd_pipe.unet.eval()
    sd_pipe.unet.requires_grad_(False)
    sd_pipe.vae.eval()
    sd_pipe.vae.requires_grad_(False)

    # load clipper - don't L2 norm the extracted CLIP embeddings since we want the prior 
    # to learn un-normed embeddings for usage with the SD image variation pipeline
    clip_extractor = Clipper(clip_variant, clamp_embs=clamp_embs, norm_embs=False)

    # # load COCO annotations curated in the same way as the mind_reader (Lin Sprague Singh) preprint
    # f = h5py.File('/scratch/gpfs/KNORMAN/nsdgeneral_hdf5/COCO_73k_subj_indices.hdf5', 'r')
    # subj01_order = f['subj01'][:]
    # f.close()
    # annots = np.load('/scratch/gpfs/KNORMAN/nsdgeneral_hdf5/COCO_73k_annots_curated.npy',allow_pickle=True)
    # subj01_annots = annots[subj01_order]

    if remote_data:
        # pull from huggingface datasets
        train_url, val_url = utils.get_huggingface_urls(data_commit)
    else:
        # local paths
        train_url = "/scratch/gpfs/KNORMAN/webdataset_nsd/webdataset_split/train/train_subj01_{0..49}.tar"
        val_url = "/scratch/gpfs/KNORMAN/webdataset_nsd/webdataset_split/val/val_subj01_0.tar"

    train_dl, val_dl = utils.get_dataloaders(
        batch_size, image_var, 
        num_workers=num_workers,
        train_url=train_url,
        val_url=val_url,
    )

    # get first batches
    for train_i, (voxel0, image0) in enumerate(train_dl):
        break
    for val_i, (val_voxel0, val_image0) in enumerate(val_dl):
        break

    # voxel2clip mapper model
    brain_net = BrainNetwork(768) 
    if using_ddp:
        brain_net0 = brain_net.to(local_rank)
        brain_net = DDP(brain_net0, device_ids=[local_rank])
    else:
        brain_net = brain_net.to(device)

    # Loading checkpoint
    print("ckpt_path", ckpt_path)
    checkpoint = torch.load(ckpt_path, map_location=device)    
    if 'model_state_dict' in checkpoint:
        brain_net.load_state_dict(checkpoint['model_state_dict'])
    else:
        brain_net.load_state_dict(checkpoint)
        
    brain_net.eval()
    brain_net.requires_grad_(False)

    if not pretrained:
        # setup prior network
        prior_network = DiffusionPriorNetwork(
            dim=dim,
            depth=depth,
            dim_head=dim_head,
            heads=heads
        ).to(device)

        # custom version that can fix seeds
        diffusion_prior = BrainDiffusionPrior(
            net=prior_network,
            image_embed_dim=dim,
            condition_on_text_encodings=condition_on_text_encodings,
            timesteps=timesteps,
            cond_drop_prob=cond_drop_prob,
            image_embed_scale=image_embed_scale,
        ).to(device)
    else:
        print("WARNING: ignoring passed values for dim, depth, dim_head, heads, "
              "cond_drop_prob, image_embed_scale")
        assert timesteps == 1000
        diffusion_prior = BrainDiffusionPrior.from_pretrained(
            dict(),
            dict(
                condition_on_text_encodings=condition_on_text_encodings,
                timesteps=timesteps,
                # cond_drop_prob=cond_drop_prob,
                # image_embed_scale=image_embed_scale,
            ),
        )

    utils.count_params(diffusion_prior)

    optimizer = torch.optim.AdamW(diffusion_prior.parameters(), lr=initial_lr)
    if lr_scheduler == 'fixed':
        lr_scheduler = None
    elif lr_scheduler == 'cycle':
        # <TODO> hard-coded values
        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer, 
            max_lr=max_lr, 
            total_steps=num_epochs*((24983//batch_size)//num_devices), 
            final_div_factor=1000,
            last_epoch=-1, pct_start=2/num_epochs
        )

    epoch = 0
    losses, val_losses, lrs = [], [], []
    sims, val_sims = [], []
    sims_base, val_sims_base = [], []
    best_val_loss = 1e9

    def save_ckpt(tag):
        ckpt_path = os.path.join(outdir, f'ckpt-{tag}.pth')
        print(f'saving {ckpt_path}')
        if (using_ddp==False) or (using_ddp==True and local_rank==0):
            state_dict = brain_net.state_dict()
            if using_ddp: # if using DDP, convert DDP state_dict to non-DDP before saving
                for key in list(state_dict.keys()):
                    if 'module.' in key:
                        state_dict[key.replace('module.', '')] = state_dict[key]
                        del state_dict[key]   
            torch.save({
                'epoch': epoch,
                'model_state_dict': diffusion_prior.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'train_losses': losses,
                'val_losses': val_losses,
                'lrs': lrs,
                'train_sims': sims,
                'val_sims': val_sims,
                }, ckpt_path)
            
            if using_ddp:
                # this tells the other gpus wait for the first gpu to finish saving the model
                dist.barrier()

    # resume from checkpoint:
    # prior_checkpoint = torch.load(ckpt_path, map_location=device)
    # diffusion_prior.load_state_dict(prior_checkpoint['model_state_dict'])
    # optimizer.load_state_dict(prior_checkpoint['optimizer_state_dict'])
    # lr = prior_checkpoint['lr']
    # epoch = prior_checkpoint['epoch']+1
    # losses = prior_checkpoint['train_losses']
    # optimizer.param_groups[0]['lr'] = lr

    if wandb_log:
        import wandb
        wandb.init(
            # set the wandb project where this run will be logged
            project=wandb_project,
            name=wandb_run_name,
            config=config,
            notes=wandb_notes,
        )

    if first_batch:
        # fake DataLoaders with just the first batches
        bs = batch_size
        train_dl = [(voxel0[:bs], image0[:bs])]
        val_dl = [(val_voxel0[:bs], val_image0[:bs])]

    # feed text and images into diffusion prior network
    progress_bar = tqdm(range(epoch, num_epochs), desc='train loop')

    for epoch in progress_bar:
        diffusion_prior.train()
        
        loss_on_aug, loss_off_aug = [], []
        image_aug = None

        for train_i, (voxel, image) in enumerate(train_dl):
            optimizer.zero_grad()
            image = image.to(device)
            clip_embed = brain_net(voxel.to(device).float())
            image_clip = clip_extractor.embed_image(image).float()

            if clip_aug_mode == 'x':
                # the target y is fixed, and we will change the input x
                if random.random() < clip_aug_prob:
                    # get an image variation
                    image_aug = sd_pipe(
                        image=image,
                        width=256,
                        height=256,
                    )
                    # get the CLIP embedding for the variation and use it for x
                    clip_aug = clip_extractor.embed_image(image_aug).float()

                    loss, pred = diffusion_prior(text_embed=clip_aug, image_embed=image_clip)
                    loss_on_aug.append(loss.item())
                else:
                    loss, pred = diffusion_prior(text_embed=clip_embed, image_embed=image_clip)
                    loss_off_aug.append(loss.item())

            elif clip_aug_mode == 'y':
                # the input x is fixed, and we will change the target y
                if random.random() < clip_aug_prob:
                    _, clip_pred = diffusion_prior(text_embed=clip_embed, image_embed=image_clip)

                    # get an image variation
                    image_aug = sd_pipe(
                        # duplicate the embedding to serve classifier free guidance
                        image_embeddings=torch.cat([torch.zeros_like(clip_pred), clip_pred]).unsqueeze(1),
                        width=256,
                        height=256,
                    )
                    # get the CLIP embedding for the variation and use it for y
                    clip_aug = clip_extractor.embed_image(image_aug).float()

                    loss, pred = diffusion_prior(text_embed=clip_embed, image_embed=clip_aug)
                    loss_on_aug.append(loss.item())
                else:
                    loss, pred = diffusion_prior(text_embed=clip_embed, image_embed=image_clip)
                    loss_off_aug.append(loss.item())
            else:
                loss, pred = diffusion_prior(text_embed=clip_embed, image_embed=image_clip)

            loss.backward()
            optimizer.step()
            if lr_scheduler is not None:
                lr_scheduler.step() 

            losses.append(loss.item())
            lrs.append(optimizer.param_groups[0]['lr'])
            sims.append(F.cosine_similarity(image_clip, pred).mean().item())
            sims_base.append(F.cosine_similarity(image_clip, clip_embed).mean().item())
            
        diffusion_prior.eval()
        for val_i, (val_voxel, val_image) in enumerate(val_dl):    
            with torch.no_grad(): 
                val_image = val_image.to(device)

                clip_embed = brain_net(val_voxel.to(device).float())
                #clip_embed = nn.functional.normalize(clip_embed,dim=-1)
                # clip_embed = clip_extractor.embed_curated_annotations(subj01_annots[voxel])

                image_clip = clip_extractor.embed_image(val_image).float()

                val_loss, val_pred = diffusion_prior(text_embed=clip_embed, image_embed=image_clip)

                val_losses.append(val_loss.item())
                val_sims.append(F.cosine_similarity(image_clip, val_pred).mean().item())
                val_sims_base.append(F.cosine_similarity(image_clip, clip_embed).mean().item())
                
        if ckpt_saving:
            # save best model
            val_loss = np.mean(val_losses[-(val_i+1):])
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                save_ckpt('best')
            else:
                print(f'not best - val_loss: {val_loss:.3f}, best_val_loss: {best_val_loss:.3f}')

            # Save model checkpoint every `ckpt_interval`` epochs or on the last epoch
            if (ckpt_interval is not None and (epoch + 1) % ckpt_interval == 0) or epoch == num_epochs - 1:
                save_ckpt(f'epoch{epoch:03d}')

        logs = OrderedDict(
            train_loss=np.mean(losses[-(train_i+1):]),
            val_loss=np.mean(val_losses[-(val_i+1):]),
            lr=lrs[-1],
            train_sim=np.mean(sims[-(train_i+1):]),
            val_sim=np.mean(val_sims[-(val_i+1):]),
        )
        progress_bar.set_postfix(**logs)

        logs = {
            "train/loss": np.mean(losses[-(train_i+1):]),
            "val/loss": np.mean(val_losses[-(val_i+1):]),
            "train/lr": lrs[-1],
            "train/cosine_sim": np.mean(sims[-(train_i+1):]),
            "val/cosine_sim": np.mean(val_sims[-(val_i+1):]),
            "train/cosine_sim_base": np.mean(sims_base[-(train_i+1):]),
            "val/cosine_sim_base": np.mean(val_sims_base[-(val_i+1):]),
            "train/num_steps": len(losses),
            "train/loss_on_aug": np.mean(loss_on_aug),
            "train/loss_off_aug": np.mean(loss_off_aug),
        }

        # sample some images
        if (not save_samples_at_end and n_samples_save > 0) or (save_samples_at_end and epoch == num_epochs - 1):
            # training
            grids = utils.sample_images(
                clip_extractor, brain_net, sd_pipe, diffusion_prior,
                voxel0[:n_samples_save], image0[:n_samples_save], seed=42,
            )
            for i, grid in enumerate(grids):
                grid.save(os.path.join(outdir, f'samples-train-{i:03d}.png'))
            if wandb_log:
                logs['train/samples'] = [wandb.Image(grid) for grid in grids]

            # validation
            grids = utils.sample_images(
                clip_extractor, brain_net, sd_pipe, diffusion_prior,
                val_voxel0[:n_samples_save], val_image0[:n_samples_save], seed=42,
            )
            for i, grid in enumerate(grids):
                grid.save(os.path.join(outdir, f'samples-val-{i:03d}.png'))
            if wandb_log:
                logs['val/samples'] = [wandb.Image(grid) for grid in grids]

        # save augmented image pairs
        if n_aug_save > 0 and image_aug is not None:
            assert image[0].shape == image_aug[0].shape, 'batch dim does not match'
            # two rows: original, augmented
            grid = utils.torch_to_Image(
                make_grid(torch.cat((
                    nn.functional.interpolate(image[:n_aug_save], (256,256), mode="area", antialias=False),
                    nn.functional.interpolate(image_aug[:n_aug_save], (256,256), mode="area", antialias=False)
                )), nrow=image[:n_aug_save].shape[0], padding=10)
            )
            grid.save(os.path.join(outdir, f'augmented-pairs.png'))
            if wandb_log:
                logs['train/samples-aug'] = wandb.Image(grid)
            
        if wandb_log:
            wandb.log(logs)
            
    if wandb_log:
        wandb.finish()