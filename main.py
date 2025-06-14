import argparse
import os
from pathlib import Path
import random
import functools

import matplotlib
import matplotlib.pyplot as plt
import network
import numpy as np
import torch
import torch.nn as nn
import utils
from datasets import Cityscapes, VOCSegmentation
from metrics import StreamSegMetrics
from PIL import Image
from torch.utils import data
from tqdm import tqdm
from utils import ext_transforms as et
from utils.visualizer import Visualizer

import wandb
import yaml


def get_argparser():
    parser = argparse.ArgumentParser()

    # Datset Options
    parser.add_argument("--data_root", type=str, default='./datasets/data',
                        help="path to Dataset")
    parser.add_argument("--dataset", type=str, default='voc',
                        choices=['voc', 'cityscapes'], help='Name of dataset')
    parser.add_argument("--num_classes", type=int, default=None,
                        help="num classes (default: None)")
    parser.add_argument("--num_workers", type=int, default=4, help="Number of workers for dataloader")

    # Deeplab Options
    available_models = sorted(name for name in network.modeling.__dict__ if name.islower() and \
                              not (name.startswith("__") or name.startswith('_')) and callable(
                              network.modeling.__dict__[name])
                              )
    parser.add_argument("--model", type=str, default='deeplabv3plus_mobilenet',
                        choices=available_models, help='model name')
    parser.add_argument("--separable_conv", action='store_true', default=False,
                        help="apply separable conv to decoder and aspp")
    parser.add_argument("--output_stride", type=int, default=16, choices=[8, 16])

    # Train Options
    parser.add_argument("--test_only", action='store_true', default=False)
    parser.add_argument("--save_val_results", action='store_true', default=False,
                        help="save segmentation results to \"./results\"")
    parser.add_argument("--total_itrs", type=int, default=30e3,
                        help="epoch number (default: 30k)")
    parser.add_argument("--n_epochs", type=int, default=None)
    parser.add_argument("--lr", type=float, default=0.01,
                        help="learning rate (default: 0.01)")
    parser.add_argument("--lr_policy", type=str, default='poly', choices=['poly', 'step'],
                        help="learning rate scheduler policy")
    parser.add_argument("--step_size", type=int, default=10000)
    parser.add_argument("--crop_val", action='store_true', default=False,
                        help='crop validation (default: False)')
    parser.add_argument("--batch_size", type=int, default=16,
                        help='batch size (default: 16)')
    parser.add_argument("--val_batch_size", type=int, default=16,
                        help='batch size for validation (default: 4)')
    parser.add_argument("--crop_size", type=int, default=513)

    parser.add_argument("--ckpt", default=None, type=str,
                        help="restore from checkpoint")
    parser.add_argument("--continue_training", action='store_true', default=False)
    parser.add_argument("--ignore_previous_best_score", action='store_true', default=False, \
        help="Save the best model based only on the score acvhieved in the current run (ignore pretrained model's score)")

    parser.add_argument("--loss_type", type=str, default='cross_entropy',
                        choices=['cross_entropy', 'focal_loss'], help="loss type (default: False)")
    parser.add_argument("--gpu_id", type=str, default='0',
                        help="GPU ID")
    parser.add_argument("--weight_decay", type=float, default=1e-4,
                        help='weight decay (default: 1e-4)')
    parser.add_argument("--random_seed", type=int, default=1,
                        help="random seed (default: 1)")
    parser.add_argument("--print_interval", type=int, default=10,
                        help="print interval of loss (default: 10)")
    parser.add_argument("--val_interval", type=int, default=None,
                        help="epoch interval for eval (default: validate every epoch)")
    parser.add_argument("--download", action='store_true', default=False,
                        help="download datasets")

    # PASCAL VOC Options
    parser.add_argument("--year", type=str, default='2012',
                        choices=['2012_aug', '2012', '2011', '2009', '2008', '2007'], help='year of VOC')

    # Visdom options
    parser.add_argument("--enable_vis", action='store_true', default=False,
                        help="use visdom for visualization")
    parser.add_argument("--vis_port", type=str, default='13570',
                        help='port for visdom')
    parser.add_argument("--vis_env", type=str, default='main',
                        help='env for visdom')
    parser.add_argument("--vis_num_samples", type=int, default=8,
                        help='number of samples for visualization (default: 8)')

    # Wandb options
    parser.add_argument("--enable_wandb", action='store_true', default=False, help="Use Weights & Biases for logging")
    parser.add_argument("--wandb_team", type=str, default=None, help="Weights & Biases team name")
    parser.add_argument("--wandb_project", type=str, default=None, help="Weights & Biases project name")
    parser.add_argument("--wandb_run_name", type=str, default=None, help="Weights & Biases current run name")
    parser.add_argument("--wandb_restore_ckpt", type=str, default=None, help="Weights & Biases current run name")
    parser.add_argument("--wandb_restore_run_path", type=str, default=None, help="Weights & Biases current run name")

    parser.add_argument("--wandb_sweep_config", type=str, help="Weights & Biases sweep config file path")
    parser.add_argument("--wandb_sweep_id", type=str, default=None, help="Weights & Biases sweep id. If provided, an existing sweep will be used instead of creating a new one.")
    return parser


def get_dataset(opts):
    """ Dataset And Augmentation
    """
    if opts.dataset == 'voc':
        train_transform = et.ExtCompose([
            # et.ExtResize(size=opts.crop_size),
            et.ExtRandomScale((0.5, 2.0)),
            et.ExtRandomCrop(size=(opts.crop_size, opts.crop_size), pad_if_needed=True),
            et.ExtRandomHorizontalFlip(),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])
        if opts.crop_val:
            val_transform = et.ExtCompose([
                et.ExtResize(opts.crop_size),
                et.ExtCenterCrop(opts.crop_size),
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        else:
            val_transform = et.ExtCompose([
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        train_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                    image_set='train', download=opts.download, transform=train_transform)
        val_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                  image_set='val', download=False, transform=val_transform)

    if opts.dataset == 'cityscapes':
        train_transform = et.ExtCompose([
            # et.ExtResize( 512 ),
            et.ExtRandomCrop(size=(opts.crop_size, opts.crop_size)),
            et.ExtColorJitter(brightness=0.5, contrast=0.5, saturation=0.5),
            et.ExtRandomHorizontalFlip(),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])

        val_transform = et.ExtCompose([
            # et.ExtResize( 512 ),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])

        train_dst = Cityscapes(root=opts.data_root,
                               split='train', transform=train_transform)
        val_dst = Cityscapes(root=opts.data_root,
                             split='val', transform=val_transform)
    return train_dst, val_dst


def validate(opts, model, loader, device, metrics, ret_samples_ids=None):
    """Do validation and return specified samples"""
    metrics.reset()
    ret_samples = []
    if opts.save_val_results:
        if not os.path.exists('results'):
            os.mkdir('results')
        denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406],
                                   std=[0.229, 0.224, 0.225])
        img_id = 0

    with torch.no_grad():
        for i, (images, labels) in enumerate(tqdm(loader, desc="Validating", leave=False)):

            images = images.to(device, dtype=torch.float32)  # noqa: PLW2901
            labels = labels.to(device, dtype=torch.long)  # noqa: PLW2901

            outputs = model(images)
            preds = outputs.detach().max(dim=1)[1].cpu().numpy()
            targets = labels.cpu().numpy()

            metrics.update(targets, preds)
            if ret_samples_ids is not None and i in ret_samples_ids:  # get vis samples
                ret_samples.append(
                    (images[0].detach().cpu().numpy(), targets[0], preds[0]))

            if opts.save_val_results:
                for j in range(len(images)):
                    image = images[j].detach().cpu().numpy()
                    target = targets[j]
                    pred = preds[j]

                    image = (denorm(image) * 255).transpose(1, 2, 0).astype(np.uint8)
                    target = loader.dataset.decode_target(target).astype(np.uint8)
                    pred = loader.dataset.decode_target(pred).astype(np.uint8)

                    Image.fromarray(image).save('results/%d_image.png' % img_id)
                    Image.fromarray(target).save('results/%d_target.png' % img_id)
                    Image.fromarray(pred).save('results/%d_pred.png' % img_id)

                    _ = plt.figure()
                    plt.imshow(image)
                    plt.axis('off')
                    plt.imshow(pred, alpha=0.7)
                    ax = plt.gca()
                    ax.xaxis.set_major_locator(matplotlib.ticker.NullLocator())
                    ax.yaxis.set_major_locator(matplotlib.ticker.NullLocator())
                    plt.savefig('results/%d_overlay.png' % img_id, bbox_inches='tight', pad_inches=0)
                    plt.close()
                    img_id += 1
        score = metrics.get_results()
    return score, ret_samples


def get_wandb_run(opts, ckpt):
    if opts.wandb_run_name is None:
        wandb_run_name = "_".join([
            ("sweep_" if opts.wandb_sweep_config or opts.wandb_sweep_id else ""),
            ckpt.split('/')[-1].split('.')[0],
            f"crop-{opts.crop_size}-outstride-{opts.output_stride}",
            f"loss-{opts.loss_type}",
            f"lr-{opts.lr}-{opts.lr_policy}",
            f"wd-{opts.weight_decay}",
            f"batch-{opts.batch_size}",
            f"iters-{opts.total_itrs}",
        ])
        if opts.test_only:
            wandb_run_name += f"test_{wandb_run_name}"
    else:
        wandb_run_name = opts.wandb_run_name

    run = wandb.init(
        project=opts.wandb_project,
        entity=opts.wandb_team,
        name=wandb_run_name,
        config=vars(opts),
    )
    return run

def _main():
    parser = get_argparser()
    opts = parser.parse_args()

    if opts.dataset.lower() == 'voc':
        opts.num_classes = 21
    elif opts.dataset.lower() == 'cityscapes':
        opts.num_classes = 19

    if opts.dataset == 'voc' and not opts.crop_val:
        opts.val_batch_size = 1

    print("Options:")
    for k, v in vars(opts).items():
        print(f"{k}: {v}")

    if opts.enable_wandb:
        wandb_run = get_wandb_run(opts, opts.ckpt)
    else:
        wandb_run = None

    if opts.wandb_sweep_config is not None or opts.wandb_sweep_id is not None:
        assert (config_lr := wandb.config.get("lr")) is not None, "Sweep config must contain 'lr' parameter"
        assert (config_weight_decay := wandb.config.get("weight_decay")) is not None, "Sweep config must contain 'weight_decay' parameter"
        assert (config_loss_type := wandb.config.get("loss_type")) is not None, "Sweep config must contain 'loss_type' parameter"
        opts.lr = config_lr
        opts.weight_decay = config_weight_decay
        opts.loss_type = config_loss_type

    wandb_run.config.update(opts, allow_val_change=True)  # Update wandb config with opts

    # Setup visualization
    vis = Visualizer(port=opts.vis_port,
                    env=opts.vis_env) if opts.enable_vis else None
    if vis is not None:  # display options
        vis.vis_table("Options", vars(opts))

    os.environ['CUDA_VISIBLE_DEVICES'] = opts.gpu_id
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("Device: %s" % device)
    # Reduce VRAM usage by reducing fragmentation
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

    # Setup random seed
    torch.manual_seed(opts.random_seed)
    np.random.seed(opts.random_seed)
    random.seed(opts.random_seed)

    # Setup dataloader
    train_dst, val_dst = get_dataset(opts)
    train_loader = data.DataLoader(
        train_dst, batch_size=opts.batch_size, shuffle=True, num_workers=opts.num_workers,
        drop_last=True, pin_memory=True, persistent_workers=True)  # drop_last=True to ignore single-image batches.
    val_loader = data.DataLoader(
        val_dst, batch_size=opts.val_batch_size, shuffle=True, num_workers=opts.num_workers, \
            pin_memory=True, persistent_workers=True)
    print("Dataset: %s, Train set: %d, Val set: %d" %
        (opts.dataset, len(train_dst), len(val_dst)))

    # Set up model (all models are 'constructed at network.modeling)
    model = network.modeling.__dict__[opts.model](num_classes=opts.num_classes, output_stride=opts.output_stride)
    if opts.separable_conv and 'plus' in opts.model:
        network.convert_to_separable_conv(model.classifier)
    utils.set_bn_momentum(model.backbone, momentum=0.01)

    # Set up metrics
    metrics = StreamSegMetrics(opts.num_classes)

    # Set up optimizer
    optimizer = torch.optim.SGD(params=[
        {'params': model.backbone.parameters(), 'lr': 0.1 * opts.lr},
        {'params': model.classifier.parameters(), 'lr': opts.lr},
    ], lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    # optimizer = torch.optim.SGD(params=model.parameters(), lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    # torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.lr_decay_step, gamma=opts.lr_decay_factor)
    if opts.lr_policy == 'poly':
        scheduler = utils.PolyLR(optimizer, opts.total_itrs, power=0.9)
    elif opts.lr_policy == 'step':
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.step_size, gamma=0.1)

    # Set up criterion
    # criterion = utils.get_loss(opts.loss_type)
    if opts.loss_type == 'focal_loss':
        criterion = utils.FocalLoss(ignore_index=255, size_average=True)
    elif opts.loss_type == 'cross_entropy':
        criterion = nn.CrossEntropyLoss(ignore_index=255, reduction='mean')

    def save_ckpt(path):
        """ save current model
        """
        torch.save({
            "cur_itrs": cur_itrs,
            "model_state": model.module.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "best_score": best_score,
        }, path)
        wandb.save(path, policy="live")
        tqdm.write("Model saved as %s" % path)

    def load_ckpt(path, model, optimizer, scheduler):
        # https://github.com/VainF/DeepLabV3Plus-Pytorch/issues/8#issuecomment-605601402, @PytaichukBohdan
        checkpoint = torch.load(path, map_location=torch.device('cpu'), weights_only=False)
        model.load_state_dict(checkpoint["model_state"])
        model = nn.DataParallel(model)
        model.to(device)
        if opts.continue_training:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            cur_itrs = checkpoint["cur_itrs"]
            best_score = checkpoint['best_score']
            print("Training state restored from %s" % path)
        print("Model restored from %s" % path)
        return {
            "model": model,
            "optimizer": optimizer,
            "scheduler": scheduler,
            "cur_itrs": cur_itrs,
            "best_score": best_score
        }

    utils.mkdir('checkpoints')
    # Restore
    assert not ((opts.ckpt and (opts.wandb_restore_ckpt or opts.wandb_restore_run_path))), "Cannot restore from both checkpoint file and wandb"
    assert bool(opts.wandb_restore_ckpt) == bool(opts.wandb_restore_run_path), "Must provide either wandb_restore_ckpt and wandb_restore_run_path or neither of them"
    best_score = 0.0
    cur_itrs = 0
    cur_epochs = 0
    if opts.ckpt is not None:
        assert os.path.isfile(opts.ckpt), "--ckpt %s does not exist" % opts.ckpt
        ckpt = opts.ckpt
    elif opts.wandb_restore_ckpt is not None:
        wandb_restored = wandb.restore(
            name=opts.wandb_restore_ckpt,
            run_path=opts.wandb_restore_run_path,
            replace=True,
            root=Path("checkpoints") / opts.wandb_restore_run_path,
        )
        ckpt = wandb_restored.name
        wandb_restored.close()
    else:
        ckpt = None

    if ckpt:
        loaded_state = load_ckpt(ckpt, model, optimizer, scheduler)
        model, optimizer, scheduler, cur_itrs = loaded_state["model"], loaded_state["optimizer"], \
            loaded_state["scheduler"], loaded_state["cur_itrs"]
        best_score = 0 if opts.ignore_previous_best_score else loaded_state["best_score"]
    else:
        print("[!] Retrain")
        model = nn.DataParallel(model)
        model.to(device)

    start_itrs = cur_itrs

    # ==========   Train Loop   ==========#
    vis_sample_id = np.random.randint(0, len(val_loader), opts.vis_num_samples,
                                    np.int32) if opts.enable_vis else None  # sample idxs for visualization
    denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])  # denormalization for ori images

    if opts.test_only:
        model.eval()
        val_score, ret_samples = validate(
            opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
        print(metrics.to_str(val_score))
        return

    interval_loss = 0
    val_interval = opts.val_interval if opts.val_interval is not None else len(train_loader)
    while True:  # cur_itrs < opts.total_itrs:
        # =====  Train  =====
        model.train()
        cur_epochs += 1
        for (images, labels) in tqdm(train_loader, desc=f"Training epoch {cur_epochs}"):
            cur_itrs += 1

            if wandb_run is not None:
                wandb_run.log({"epoch": cur_epochs}, step=cur_itrs)

            images = images.to(device, dtype=torch.float32)  # noqa: PLW2901
            labels = labels.to(device, dtype=torch.long)  # noqa: PLW2901

            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()

            np_loss = loss.detach().cpu().numpy()
            interval_loss += np_loss
            if vis is not None:
                vis.vis_scalar('Loss', cur_itrs, np_loss)

            if wandb_run is not None:
                wandb_run.log({"train_loss": np_loss}, step=cur_itrs)

            if (cur_itrs) % 10 == 0:
                interval_loss = interval_loss / 10
                tqdm.write("Epoch %d, Itrs %d/%d, Loss=%f" %
                    (cur_epochs, cur_itrs, opts.total_itrs, interval_loss))
                interval_loss = 0.0

            if ((cur_itrs - start_itrs) % len(train_loader)) % val_interval == 0:
                save_ckpt('checkpoints/latest_%s_%s_os%d.pth' %
                        (opts.model, opts.dataset, opts.output_stride))
                tqdm.write("validation...")
                model.eval()
                val_score, ret_samples = validate(
                    opts=opts, model=model, loader=val_loader, device=device, metrics=metrics,
                    ret_samples_ids=vis_sample_id)
                tqdm.write(metrics.to_str(val_score))

                if wandb_run is not None:
                    wandb_run.log({
                        "val_" + k.replace(" ", "_"): v for k, v in val_score.items()
                    }, step=cur_itrs)

                if val_score['Mean IoU'] > best_score:  # save best model
                    best_score = val_score['Mean IoU']
                    save_ckpt('checkpoints/best_%s_%s_os%d.pth' %
                            (opts.model, opts.dataset, opts.output_stride))

                if vis is not None:  # visualize validation score and samples
                    vis.vis_scalar("[Val] Overall Acc", cur_itrs, val_score['Overall Acc'])
                    vis.vis_scalar("[Val] Mean IoU", cur_itrs, val_score['Mean IoU'])
                    vis.vis_table("[Val] Class IoU", val_score['Class IoU'])

                    for k, (img, target, lbl) in enumerate(ret_samples):
                        img = (denorm(img) * 255).astype(np.uint8)  # noqa: PLW2901
                        target = train_dst.decode_target(target).transpose(2, 0, 1).astype(np.uint8)  # noqa: PLW2901
                        lbl = train_dst.decode_target(lbl).transpose(2, 0, 1).astype(np.uint8)  # noqa: PLW2901
                        concat_img = np.concatenate((img, target, lbl), axis=2)  # concat along width
                        vis.vis_image('Sample %d' % k, concat_img)
                model.train()
            scheduler.step()

            if cur_itrs >= opts.total_itrs:
                if wandb_run is not None:
                    wandb_run.finish()
                return
        if cur_epochs >= opts.n_epochs:
            if wandb_run is not None:
                wandb_run.finish()
            return


def main():
    parser = get_argparser()
    opts = parser.parse_args()

    assert opts.enable_wandb and opts.wandb_project is not None and opts.wandb_team is not None, \
        "get_wandb_run was called, but not all required arguments were provided."

    if opts.enable_wandb:
        WANDB_TOKEN = os.getenv("WANDB_TOKEN")
        assert WANDB_TOKEN, "WANDB_TOKEN environment variable not set. Please set it to your Weights & Biases API key."
        wandb.login(key=WANDB_TOKEN, verify=True)


    assert opts.wandb_sweep_config is None or opts.wandb_sweep_id is None, \
        "You cannot provide both wandb_sweep_config and wandb_sweep_id. Please provide only one of them."

    if opts.wandb_sweep_config is not None:
        assert opts.enable_wandb, "You must enable Weights & Biases to use sweeps."
        assert opts.wandb_project is not None, "You must specify a wandb project name to use sweeps."
        assert opts.wandb_team is not None, "You must specify a wandb team name to use sweeps."
        with open(opts.wandb_sweep_config, "r") as f:
            sweep_config = yaml.safe_load(f)
        sweep_id = wandb.sweep(sweep_config, project=opts.wandb_project, entity=opts.wandb_team)
        print(f"Created sweep with id: {sweep_id}")
    elif opts.wandb_sweep_id is not None:
        assert opts.enable_wandb, "You must enable Weights & Biases to use sweeps."
        assert opts.wandb_project is not None, "You must specify a wandb project name to use sweeps."
        assert opts.wandb_team is not None, "You must specify a wandb team name to use sweeps."
        sweep_id = opts.wandb_sweep_id
    else:
        sweep_id = None


    if sweep_id is not None:
        wandb.agent(sweep_id, function=_main, project=opts.wandb_project, entity=opts.wandb_team)
    else:
        _main()

if __name__ == '__main__':
    main()
