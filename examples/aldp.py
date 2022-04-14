# Import modules
import argparse
import os
import torch
import numpy as np

import normflow as nf
import boltzgen as bg

from time import time
from fab.utils.training import load_config
from fab.target_distributions.aldp import AldpBoltzmann
from fab import FABModel
from fab.wrappers.normflow import WrappedNormFlowModel
from fab.sampling_methods.transition_operators import HamiltoneanMonteCarlo, Metropolis
from fab.utils.aldp import evaluateAldp
from fab.utils.numerical import effective_sample_size



# Parse input arguments
parser = argparse.ArgumentParser(description='Train Boltzmann Generator with varying '
                                             'base distribution')

parser.add_argument('--config', type=str, default='../config/bm.yaml',
                    help='Path config file specifying model '
                         'architecture and training procedure')
parser.add_argument("--resume", action="store_true",
                    help='Flag whether to resume training')
parser.add_argument("--tlimit", type=float, default=None,
                    help='Number of hours after which to stop training')
parser.add_argument('--mode', type=str, default='gpu',
                    help='Compute mode, can be cpu, or gpu')
parser.add_argument('--precision', type=str, default='double',
                    help='Precision to be used for computation, '
                         'can be float, double, or mixed')

args = parser.parse_args()

# Load config
config = load_config(args.config)

# Precision
if args.precision == 'double':
    torch.set_default_dtype(torch.float64)

# Set seed
if 'seed' in config['training'] and config['training']['seed'] is not None:
    torch.manual_seed(config['training']['seed'])

# GPU usage
use_gpu = not args.mode == 'cpu' and torch.cuda.is_available()
device = torch.device('cuda' if use_gpu else 'cpu')

# Load data
path = config['data']['test']
test_data = torch.load(path)
if args.precision == 'double':
    test_data = test_data.double()
else:
    test_data = test_data.float()
test_data = test_data.to(device)


# Set up model

# Target distribution
transform_mode = 'mixed' if not 'transform' in config['system'] \
    else config['system']['transform']
target = AldpBoltzmann(data_path=config['data']['transform'],
                       temperature=config['system']['temperature'],
                       energy_cut=config['system']['energy_cut'],
                       energy_max=config['system']['energy_max'],
                       n_threads=config['system']['n_threads'],
                       transform=transform_mode)
target = target.to(device)

# Flow
flow_type = config['flow']['type']
ndim = 60
# Flow layers
layers = []

ncarts = target.coordinate_transform.transform.len_cart_inds
permute_inv = target.coordinate_transform.transform.permute_inv.cpu().numpy()
dih_ind_ = target.coordinate_transform.transform.ic_transform.dih_indices.cpu().numpy()
std_dih = target.coordinate_transform.transform.ic_transform.std_dih.cpu()

ind = np.arange(ndim)
ind = np.concatenate([ind[:3 * ncarts - 6], -np.ones(6, dtype=np.int), ind[3 * ncarts - 6:]])
ind = ind[permute_inv]
dih_ind = ind[dih_ind_]

ind_circ_ = std_dih > 0.5
ind_circ = dih_ind[ind_circ_]
bound_circ = np.pi / std_dih[ind_circ_]

tail_bound = 5. * torch.ones(ndim)
tail_bound[ind_circ] = bound_circ

for i in range(config['flow']['blocks']):
    if flow_type == 'rnvp':
        # Coupling layer
        hl = config['flow']['hidden_layers'] * [config['flow']['hidden_units']]
        scale_map = config['flow']['scale_map']
        scale = scale_map is not None
        if scale_map == 'tanh':
            output_fn = 'tanh'
            scale_map = 'exp'
        else:
            output_fn = None
        param_map = nf.nets.MLP([(ndim + 1) // 2] + hl + [(ndim // 2) * (2 if scale else 1)],
                                init_zeros=config['flow']['init_zeros'], output_fn=output_fn)
        layers.append(nf.flows.AffineCouplingBlock(param_map, scale=scale,
                                                   scale_map=scale_map))
    elif flow_type == 'circular-ar-nsf':
        bl = config['flow']['blocks_per_layer']
        hu = config['flow']['hidden_units']
        nb = config['flow']['num_bins']
        ii = config['flow']['init_identity']
        dropout = config['flow']['dropout']
        layers.append(nf.flows.CircularAutoregressiveRationalQuadraticSpline(ndim,
            bl, hu, ind_circ, tail_bound=tail_bound, num_bins=nb, permute_mask=True,
            init_identity=ii, dropout_probability=dropout))
    elif flow_type == 'circular-coup-nsf':
        bl = config['flow']['blocks_per_layer']
        hu = config['flow']['hidden_units']
        nb = config['flow']['num_bins']
        ii = config['flow']['init_identity']
        dropout = config['flow']['dropout']
        if i % 2 == 0:
            mask = nf.utils.masks.create_random_binary_mask(ndim)
        else:
            mask = 1 - mask
        layers.append(nf.flows.CircularAutoregressiveRationalQuadraticSpline(ndim,
            bl, hu, ind_circ, tail_bound=tail_bound, num_bins=nb, init_identity=ii,
            dropout_probability=dropout, mask=mask))
    else:
        raise NotImplementedError('The flow type ' + flow_type + ' is not implemented.')

    if config['flow']['mixing'] == 'affine':
        layers.append(nf.flows.InvertibleAffine(ndim, use_lu=True))
    elif config['flow']['mixing'] == 'permute':
        layers.append(nf.flows.Permute(ndim))

    if config['flow']['actnorm']:
        layers.append(nf.flows.ActNorm(ndim))

# Map input to periodic interval
layers.append(nf.flows.Periodic(ind_circ, bound_circ))

# Base distribution
if config['flow']['base']['type'] == 'gauss':
    base = nf.distributions.DiagGaussian(ndim,
                                         trainable=config['flow']['base']['learn_mean_var'])
elif config['flow']['base']['type'] == 'gauss-uni':
    base_scale = torch.ones(ndim)
    base_scale[ind_circ] = bound_circ * 2
    base = nf.distributions.UniformGaussian(ndim, ind_circ, scale=base_scale)
    base.shape = (ndim,)
else:
    raise NotImplementedError('The base distribution ' + config['flow']['base']['type']
                              + ' is not implemented')
flow = nf.NormalizingFlow(base, layers)
wrapped_flow = WrappedNormFlowModel(flow).to(device)

# Transition operator
if config['fab']['transition_type'] == 'hmc':
    # very lightweight HMC.
    transition_operator = HamiltoneanMonteCarlo(
        n_ais_intermediate_distributions=config['fab']['n_int_dist'],
        dim=ndim, L=config['fab']['n_inner'])
elif config['fab']['transition_type'] == 'metropolis':
    transition_operator = Metropolis(n_transitions=config['fab']['n_int_dist'],
                                     n_updates=config['fab']['n_inner'],
                                     max_step_size=config['fab']['max_step_size'],
                                     min_step_size=config['fab']['min_step_size'],
                                     adjust_step_size=config['fab']['adjust_step_size'])
else:
    raise NotImplementedError('The transition operator ' + config['fab']['transition_type']
                              + ' is not implemented')
transition_operator = transition_operator.to(device)

# FAB model
loss_type = 'alpha_2_div' if 'loss_type' not in config['fab'] \
    else config['fab']['loss_type']
model = FABModel(flow=wrapped_flow,
                 target_distribution=target,
                 n_intermediate_distributions=config['fab']['n_int_dist'],
                 transition_operator=transition_operator,
                 loss_type=loss_type)

# Prepare output directories
root = config['training']['save_root']
cp_dir = os.path.join(root, 'checkpoints')
plot_dir = os.path.join(root, 'plots')
plot_dir_flow = os.path.join(plot_dir, 'flow')
plot_dir_ais = os.path.join(plot_dir, 'ais')
log_dir = os.path.join(root, 'log')
log_dir_flow = os.path.join(log_dir, 'flow')
log_dir_ais = os.path.join(log_dir, 'ais')
# Create dirs if not existent
for dir in [cp_dir, plot_dir, log_dir, plot_dir_flow,
            plot_dir_ais, log_dir_flow, log_dir_ais]:
    if not os.path.isdir(dir):
        os.mkdir(dir)

# Initialize optimizer and its parameters
lr = config['training']['learning_rate']
weight_decay = config['training']['weight_decay']
optimizer_name = 'adam' if not 'optimizer' in config['training'] \
    else config['training']['optimizer']
optimizer_param = model.parameters()
if optimizer_name == 'adam':
    optimizer = torch.optim.Adam(optimizer_param, lr=lr, weight_decay=weight_decay)
elif optimizer_name == 'adamax':
    optimizer = torch.optim.Adamax(optimizer_param, lr=lr, weight_decay=weight_decay)
else:
    raise NotImplementedError('The optimizer ' + optimizer_name + ' is not implemented.')
lr_warmup = 'warmup_iter' in config['training'] \
            and config['training']['warmup_iter'] is not None
if lr_warmup:
    warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer,
                                                         lambda s: min(1., s / config['training']['warmup_iter']))
if 'lr_scheduler' in config['training']:
    if config['training']['lr_scheduler']['type'] == 'exponential':
        lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer=optimizer,
            gamma=config['training']['lr_scheduler']['rate_decay'])
        lr_step = config['training']['lr_scheduler']['decay_iter']
    elif config['training']['lr_scheduler']['type'] == 'cosine':
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer,
            T_max=config['training']['max_iter'])
        lr_step = 1
    elif config['training']['lr_scheduler']['type'] == 'cosine_restart':
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer=optimizer,
            T_0=config['training']['lr_scheduler']['restart_iter'])
        lr_step = 1
else:
    lr_scheduler = None


# Train model
max_iter = config['training']['max_iter']
log_iter = config['training']['log_iter']
checkpoint_iter = config['training']['checkpoint_iter']

batch_size = config['training']['batch_size']
loss_hist = np.zeros((0, 2))
ess_hist = np.zeros((0, 3))
eval_samples = config['training']['eval_samples']
eval_batches = (eval_samples - 1) // batch_size + 1

max_grad_norm = None if not 'max_grad_norm' in config['training'] \
    else config['training']['max_grad_norm']
grad_clipping = max_grad_norm is not None
if grad_clipping:
    grad_norm_hist = np.zeros((0, 2))

# Load train data if needed
lam_fkld = None if not 'lam_fkld' in config['fab'] else config['fab']['lam_fkld']
if loss_type == 'flow_forward_kl' or lam_fkld is not None:
    path = config['data']['train']
    train_data = torch.load(path)
    if args.precision == 'double':
        train_data = train_data.double()
    else:
        train_data = train_data.float()
    train_loader = torch.utils.data.DataLoader(train_data, batch_size=batch_size,
                                               shuffle=True, pin_memory=True,
                                               drop_last=True, num_workers=4)
    train_iter = iter(train_loader)

# Resume training if needed
start_iter = 0
if args.resume:
    latest_cp = bg.utils.get_latest_checkpoint(cp_dir, 'model')
    if latest_cp is not None:
        # Load model
        model.load(latest_cp)
        start_iter = int(latest_cp[-10:-3])
        # Load optimizer
        optimizer_path = os.path.join(cp_dir, 'optimizer.pt')
        if os.path.exists(optimizer_path):
            optimizer.load_state_dict(torch.load(optimizer_path))
        # Load scheduler
        warmup_scheduler_path = os.path.join(cp_dir, 'warmup_scheduler.pt')
        if os.path.exists(warmup_scheduler_path):
            warmup_scheduler.load_state_dict(torch.load(warmup_scheduler_path))
        lr_scheduler_path = os.path.join(cp_dir, 'lr_scheduler.pt')
        if lr_scheduler is not None and os.path.exists(lr_scheduler_path):
            lr_scheduler.load_state_dict(torch.load(lr_scheduler_path))
        # Load logs
        log_labels = ['loss', 'ess']
        log_hists = [loss_hist, ess_hist]
        if grad_clipping:
            log_labels.append('grad_norm')
            log_hists.append(grad_norm_hist)
        for log_label, log_hist in zip(log_labels, log_hists):
            log_path = os.path.join(log_dir, log_label + '.csv')
            if os.path.exists(log_path):
                log_hist_ = np.loadtxt(log_path, delimiter=',', skiprows=1)
                if log_hist_.ndim == 1:
                    log_hist_ = log_hist_[None, :]
                log_hist.resize(*log_hist_.shape, refcheck=False)
                log_hist[:, :] = log_hist_
                log_hist.resize(np.sum(log_hist_[:, 0] <= start_iter), log_hist_.shape[1],
                                refcheck=False)

# Start training
start_time = time()

for it in range(start_iter, max_iter):
    # Get loss
    if loss_type == 'flow_forward_kl' or lam_fkld is not None:
        try:
            x = next(train_iter)
        except StopIteration:
            train_iter = iter(train_loader)
            x = next(train_iter)
        x = x.to(device, non_blocking=True)
        if lam_fkld is None:
            loss = model.loss(x)
        else:
            loss = model.loss(batch_size) + lam_fkld * model.flow_forward_kl(x)
    else:
        loss = model.loss(batch_size)

    # Make step
    if not torch.isnan(loss) and not torch.isinf(loss):
        loss.backward()
        if grad_clipping:
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(),
                                                       max_grad_norm)
            grad_norm_append = np.array([[it + 1, grad_norm.item()]])
            grad_norm_hist = np.concatenate([grad_norm_hist,
                                             grad_norm_append])
        optimizer.step()

    # Update Lipschitz constant if flows are residual
    if flow_type == 'residual':
        nf.utils.update_lipschitz(model, 5)

    # Log loss
    loss_append = np.array([[it + 1, loss.item()]])
    loss_hist = np.concatenate([loss_hist, loss_append])

    # Clear gradients
    nf.utils.clear_grad(model)

    # Do lr warmup if needed
    if lr_warmup and it <= config['training']['warmup_iter']:
        warmup_scheduler.step()

    # Update lr scheduler
    if lr_scheduler is not None and (it + 1) % lr_step == 0:
        lr_scheduler.step()

    # Save loss
    if (it + 1) % log_iter == 0:
        # Loss
        np.savetxt(os.path.join(log_dir, 'loss.csv'), loss_hist,
                   delimiter=',', header='it,loss', comments='')
        # Gradient clipping
        if grad_clipping:
            np.savetxt(os.path.join(log_dir, 'grad_norm.csv'),
                       grad_norm_hist, delimiter=',',
                       header='it,grad_norm', comments='')
        # Effective sample size
        if config['fab']['transition_type'] == 'hmc':
            base_samples, base_log_w, ais_samples, ais_log_w = \
                model.annealed_importance_sampler.generate_eval_data(8 * batch_size,
                                                                     batch_size)
        else:
            with torch.no_grad():
                base_samples, base_log_w, ais_samples, ais_log_w = \
                    model.annealed_importance_sampler.generate_eval_data(8 * batch_size,
                                                                         batch_size)
        ess_append = np.array([[it + 1, effective_sample_size(base_log_w, normalised=False),
                                effective_sample_size(ais_log_w, normalised=False)]])
        ess_hist = np.concatenate([ess_hist, ess_append])
        np.savetxt(os.path.join(log_dir, 'ess.csv'), ess_hist,
                   delimiter=',', header='it,flow,ais', comments='')
        if use_gpu:
            torch.cuda.empty_cache()

    if (it + 1) % checkpoint_iter == 0:
        # Save checkpoint
        model.save(os.path.join(cp_dir, 'model_%07i.pt' % (it + 1)))
        torch.save(optimizer.state_dict(),
                   os.path.join(cp_dir, 'optimizer.pt'))
        if lr_warmup:
            torch.save(warmup_scheduler.state_dict(),
                       os.path.join(cp_dir, 'warmup_scheduler.pt'))
        if lr_scheduler is not None:
            torch.save(lr_scheduler.state_dict(),
                       os.path.join(cp_dir, 'lr_scheduler.pt'))

        # Draw samples
        z_samples = torch.zeros(0, ndim).to(device)
        for i in range(eval_batches):
            if i == eval_batches - 1:
                ns = ((eval_samples - 1) % batch_size) + 1
            else:
                ns = batch_size
            if config['fab']['transition_type'] == 'hmc':
                z_ = model.flow.sample((ns,))
            else:
                with torch.no_grad():
                    z_ = model.flow.sample((ns,))
            z_samples = torch.cat((z_samples, z_.detach()))

        # Evaluate model and save plots
        evaluateAldp(z_samples, test_data, model.flow.log_prob,
                     target.coordinate_transform, it, metric_dir=log_dir_flow,
                     plot_dir=plot_dir_flow)

        # Draw samples
        z_samples = torch.zeros(0, ndim).to(device)
        for i in range(eval_batches):
            if i == eval_batches - 1:
                ns = ((eval_samples - 1) % batch_size) + 1
            else:
                ns = batch_size
            if config['fab']['transition_type'] == 'hmc':
                z_ = model.annealed_importance_sampler.sample_and_log_weights(ns,
                                                                              logging=False)[0]
            else:
                with torch.no_grad():
                    z_ = model.annealed_importance_sampler.sample_and_log_weights(ns,
                                                                                  logging=False)[0]
            z_, _ = model.flow._nf_model.flows[-1].inverse(z_.detach())
            z_samples = torch.cat((z_samples, z_.detach()))

        # Evaluate model and save plots
        evaluateAldp(z_samples, test_data, model.flow.log_prob,
                     target.coordinate_transform, it, metric_dir=log_dir_ais,
                     plot_dir=plot_dir_ais)

    # End job if necessary
    if it % checkpoint_iter == 0 and args.tlimit is not None:
        time_past = (time() - start_time) / 3600
        num_cp = (it + 1 - start_iter) / checkpoint_iter
        if num_cp > .5 and time_past * (1 + 1 / num_cp) > args.tlimit:
            break
