import os
import argparse
import yaml
import math
import time
from tqdm import tqdm
import torch
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data import DataLoader
from utils import check_file
from loss import ComputeLoss
from datasets import CustomDataset, get_data_path
from models.yolo import Model, Detect
from evaluate import evaluate, intersect_dicts


def train(opt):
    # Device
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    # Hyperparameters
    with open(opt.hyp) as f:
        hyp = yaml.load(f, Loader=yaml.SafeLoader)  # load hyps

    with open(opt.data) as f:
        data_dict = yaml.load(f, Loader=yaml.SafeLoader)  # load hyps

    # Dataset Dataloader
    train_img_path, train_label_path, val_img_path, val_label_path = get_data_path(data_dict)
    train_dataset = CustomDataset(train_img_path, train_label_path)
    val_dataset = CustomDataset(val_img_path, val_label_path)
    nw = min([os.cpu_count(), opt.batch_size if opt.batch_size > 1 else 0, 8])
    # nw = 1  # for debug
    train_dataloader = DataLoader(train_dataset,
                                batch_size=opt.batch_size,
                                shuffle=True,
                                num_workers=nw,
                                pin_memory=True,
                                collate_fn=CustomDataset.collate_fn)
    val_dataloader = DataLoader(val_dataset,
                                  batch_size=opt.batch_size,
                                  shuffle=True,
                                  num_workers=nw,
                                  pin_memory=True,
                                  collate_fn=CustomDataset.collate_fn)
    nb = len(train_dataloader)  # number of batches

    # Model
    weights = opt.weights
    pretrained = weights.endswith('.pt') or weights.endswith('.pth')
    if pretrained:
        ckpt = torch.load(weights, map_location=device, weights_only=False)  # load checkpoint
        model = Model(opt.cfg, ch=3).to(device)  # create
        exclude = ['anchor'] if opt.cfg else []  # exclude keys
        # state_dict = ckpt['model'].float().state_dict()  # official model, to FP32
        state_dict_ = ckpt['model'].float().state_dict()  # self model
        state_dict = intersect_dicts(state_dict_, model.state_dict(), exclude=exclude)  # intersect
        model.load_state_dict(state_dict, strict=False)  # load param

        model_state = model.state_dict()
        mismatch = False
        for key in state_dict:
            if not torch.allclose(model_state[key], state_dict[key], atol=1e-6):
                print(f"Weight mismatch found for {key}: difference={torch.abs(model_state[key] - state_dict[key]).max()}")
                mismatch = True
        if not mismatch:
            print("All weights match between the model and the checkpoint!")
    else:
        model = Model(opt.cfg, ch=3).to(device)  # create

    for k, v in model.named_parameters():
        if 'model.24' in k:
            v.requires_grad = True
        else:
            v.requires_grad = False  # train all layers
        print(k, v.requires_grad)

    # Config
    nc = int(data_dict['nc'])  # number of classes
    nl = model.model[-1].nl  # number of detection layers (used for scaling hyp['obj'])
    imgsz = opt.img_size[0] if isinstance(opt.img_size, list) else opt.img_size
    if not os.path.exists("result"):
        os.mkdir("result")
    save_best = 0

    # Model parameters
    hyp['box'] *= 3. / nl  # scale to layers
    hyp['cls'] *= nc / 80. * 3. / nl  # scale to classes and layers
    hyp['obj'] *= (imgsz / 640) ** 2 * 3. / nl  # scale to image size and layers
    model.nc = nc  # attach number of classes to model
    model.hyp = hyp  # attach hyperparameters to model
    model.gr = 1.0  # iou loss ratio (obj_loss = 1.0 or iou)

    # Optimizer
    if opt.adam:
        optimizer = optim.Adam(model.parameters(), lr=hyp['lr0'], betas=(hyp['momentum'],
                                                                         0.999))  # adjust beta1 to momentum
    else:
        optimizer = optim.SGD(model.parameters(), lr=hyp['lr0'], momentum=hyp['momentum'])

    # Lr strategy
    lf = lambda x: ((1 - math.cos(x * math.pi / opt.epochs)) / 2) * (hyp['lrf'] - 1) + 1
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)

    # Loss
    loss_callable = ComputeLoss(model, opt.custom_loss)

    # Train
    for epoch in range(opt.epochs):
        model.train()
        pbar = tqdm(train_dataloader, total=nb)
        for step, (imgs, targets) in enumerate(pbar):
            imgs = imgs.to(device)
            targets = targets.to(device)
            pred = model(imgs)  # forward
            loss, loss_detail = loss_callable(pred, targets)

            loss.backward()  # backward, compute gradient
            optimizer.step()  # update model param
            optimizer.zero_grad()  # clear previous grad

            info = "epoch:{} -- step:{} -- loss:{:0.5f} [lbox:{:0.5f} - lobj:{:0.5f} - lcls:{:0.5f}]".format(
                epoch, step, loss[0], loss_detail[0], loss_detail[1], loss_detail[2])
            pbar.set_description(info)
            # end batch ------------------------------------------------------------------------------------------------
        # end epoch ----------------------------------------------------------------------------------------------------

        # print("lr: ", scheduler.get_last_lr())
        scheduler.step()  # update lr

        # Evaluate
        mp, mr, map50, map_ = evaluate(opt.data, model=model, dataloader=val_dataloader)

        # Save model
        if opt.save and save_best < map50:
            save_best = map50
            t = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
            # torch.save(model.state_dict(), "result/epoch{}_{}_model.pth".format(epoch, t))  # model.model.state_dict()
            torch.save(model, "result/epoch{}_{}_model.pth".format(epoch, t))

    print("train completely.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default='yolov5xu.pt', help='pretrained model')
    parser.add_argument('--data', type=str, default='data/yolo_voc.yaml', help='data.yaml path')
    parser.add_argument('--cfg', type=str, default='models/yolov5s_yolo_voc.yaml', help='model.yaml path')
    parser.add_argument('--hyp', type=str, default='data/hyp.yolo_voc.yaml', help='hyperparameters yaml path')
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--batch-size', type=int, default=32, help='total batch size for all GPUs')
    parser.add_argument('--img-size', nargs='+', type=int, default=[640, 640], help='[train, test] image sizes')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--adam', action='store_true', help='use torch.optim.Adam() optimizer')  # if use adam optim
    parser.add_argument('--save', action='store_false', help='save to result dir default')  # save train model, default
    parser.add_argument('--custom_loss', action='store_true', help='use custom loss')  # use custom loss
    opt = parser.parse_args()

    opt.data, opt.cfg, opt.hyp = check_file(opt.data), check_file(opt.cfg), check_file(opt.hyp)  # check files
    assert len(opt.data) or len(opt.cfg) or len(opt.hyp), 'either --data or either --cfg or --hyp must be specified'

    train(opt)  # for train