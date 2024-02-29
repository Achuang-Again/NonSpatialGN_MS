import sys
import numpy as np
# from tqdm import tqdm
import mindspore as ms
import mindspore.nn as nn
import mindspore.context as context

# todo
# from torch.utils.tensorboard import SummaryWriter

# import dgl
# from dgl.data import LegacyTUDataset
# from tu_dataloader import TUDataLoader
import mindspore.dataset as ds
from mindspore_gl.dataset import Enzymes
from mindspore_gl.dataloader import RandomBatchSampler
from mindspore_gl import BatchedGraph, BatchedGraphField, GNNCell

from tqdm import tqdm
import sys
sys.path.append('..')
from MyNet import MyNet as Net
from src.utils import TrainOneStepCellWithGradClipping
from src.dataset import MultiHomoGraphDataset
from utils.config import process_config, get_args
# from utils.basis_transform import basis_transform
# from utils.config import get_config_from_json
# import random
import time

class LossNet(GNNCell):
    """ LossNet definition """
    def __init__(self, net):
        super().__init__()
        self.net = net
        self.loss_fn = nn.loss.CrossEntropyLoss()

    def construct(self, x, label, g: BatchedGraph):
        pred = self.net(x, g)
        return self.net.loss(pred, label, g)

def train(train_net, optimizer, trainloader, lossNet, config):
    train_net.set_train(True)

    train_loss, total_iter = 0, 0
    for data in trainloader:
        # optimizer.zero_grad()
        label, node_feat, row, col, node_count, edge_count, node_map_idx, edge_map_idx, graph_mask = data
        batch_homo = BatchedGraphField(row, col, node_count, edge_count, node_map_idx, edge_map_idx, graph_mask)
        train_loss += train_net(node_feat, label, *batch_homo.get_batched_graph())
        # batch_graphs = batch_graphs
        # batch_labels = batch_labels.long()
        # feat = batch_graphs.ndata.pop('feat')
        # bases = batch_graphs.edata.pop('bases')
        #
        # outputs = model(batch_graphs, feat, bases)
        # loss = lossNet(outputs, batch_labels)
        # loss.backward()
        # optimizer.step()
        # total_loss += loss.detach().item()
    train_loss /= total_iter

    return train_loss / len(trainloader)


def eval(train_net, dataloader, lossNet):
    train_net.set_train(False)
    total = 0
    total_loss = 0
    total_correct = 0

    for data in dataloader:
        graphs, labels = data
        feat = graphs.ndata.pop('feat')
        bases = graphs.edata.pop('bases')
        total += len(labels)

        outputs = train_net(graphs, feat, bases)

        _, predicted = ms.ops.max(outputs.data, 1)
        total_correct += (predicted == labels.data).sum().detach().item()

        loss = lossNet(outputs, labels)
        # crossentropy(reduce=True) for default
        total_loss += loss.detach().item() * len(labels)

    loss, acc = 1.0 * total_loss / total, 1.0 * total_correct / total
    return loss, acc


def run_given_fold(num_feature,
                   num_classes,
                   num_basis,
                   train_loader,
                   val_loader,
                   ts_kf_algo_hp,
                   config):
    model = Net(num_feature, num_classes, num_basis, config=config.architecture)

    num_params = sum(p.numel() for p in model.trainable_params())
    print(f'#Params: {num_params}')

    # https://www.mindspore.cn/docs/zh-CN/r2.0/note/api_mapping/pytorch_diff/PiecewiseConstantLR.html
    # https://www.mindspore.cn/docs/zh-CN/r2.0/api_python/mindspore.nn.html#learningrateschedule%E7%B1%BB
    # scheduler = ms.nn.piecewise_constant_lr(optimizer, step_size=config.hyperparams.step_size,
    #                                             gamma=config.hyperparams.decay_rate)
    lr_milestone = [config.hyperparams.step_size]
    learning_rates = [config.hyperparams.learning_rate]
    while lr_milestone[-1] < config.hyperparams.epochs:
        lr_milestone.append(lr_milestone[-1] + config.hyperparams.step_size)
        learning_rates.append(learning_rates[-1] * config.hyperparams.decay_rate)
    optimizer = ms.nn.Adam(model.trainable_params(),
                           lr=ms.nn.piecewise_constant_lr(milestone=lr_milestone, learning_rates=learning_rates))

    loss = LossNet(model)
    train_net = nn.TrainOneStepCell(loss, optimizer)

    train_losses = []
    train_accs = []
    test_accs = []
    for epoch in range(1, config.hyperparams.epochs):
        train_start_time = time.time()
        train_loss = train(model, optimizer, train_loader, loss, config)
        train_end_time = time.time()

        train_loss, train_acc = eval(train_net, train_loader, loss)
        valid_loss, test_acc = eval(train_net, val_loader, loss)

        print('Epoch {}, Time {:.3f} s, Train loss {}, Train acc {:.3f}, '
              'Val acc {:.3f}'.format(epoch, train_end_time - train_start_time, train_loss, train_acc, test_acc))
        # scheduler.step()

        train_losses.append(train_loss)
        train_accs.append(train_acc)
        test_accs.append(test_acc)

        print('Epoch: {:03d}, Train Loss: {:.7f}, '
              'Train Acc: {:.7f}, Test Acc: {:.7f}'.format(epoch, train_loss,
                                                           train_acc, test_acc))

        # writer.add_scalars('traP', {ts_kf_algo_hp: train_acc}, epoch)
        # # writer.add_scalars('valP', {ts_kf_algo_hp: valid_acc}, epoch)
        # writer.add_scalars('tstP', {ts_kf_algo_hp: test_acc}, epoch)
        # writer.add_scalars('traL', {ts_kf_algo_hp: train_loss}, epoch)
        # # writer.add_scalars('lr',   {ts_kf_algo_hp: lr}, epoch)

    return test_accs, train_losses, train_accs


def run_model(config):
    if config.get('seed') is not None:
        ms.set_seed(config.seed)
        # random.seed(config.seed)
        # torch.manual_seed(config.seed)
        # np.random.seed(config.seed)
        # if torch.cuda.is_available():
        #     torch.cuda.manual_seed_all(config.seed)

    folds_test_accs = []
    folds_train_losses = []
    folds_train_accs = []

    def k_folds_average(avg_folds):
        avg_folds = np.vstack(avg_folds)
        return np.mean(avg_folds, axis=0), np.std(avg_folds, axis=0)

    algo_hp = str(config.commit_id[0:7]) + '_' \
              + str(config.basis) \
              + 'E' + str(config.epsilon) \
              + 'P' + str(config.power) \
              + 'I' + str(config.get('identity', 1)) + '_' \
              + str(config.architecture.nonlinear) + '_' \
              + str(config.architecture.pooling) + '_' \
              + str(config.architecture.layers) + '_' \
              + str(config.architecture.hidden) + '_' \
              + str(config.architecture.dropout) + '_' \
              + str(config.hyperparams.learning_rate) + '_' \
              + str(config.hyperparams.step_size) + '_' \
              + str(config.hyperparams.decay_rate) + '_' \
              + 'B' + str(config.hyperparams.batch_size) \
              + 'S' + str(config.seed) \
              + 'W' + str(config.get('num_workers', 'na'))

    # dataset = LegacyTUDataset(config.dataset_name, raw_dir='./dataset')
    dataset = Enzymes('./dataset/ENZYMES/ENZYMES')
    train_batch_sampler = RandomBatchSampler(dataset.train_graphs, batch_size=config.hyperparams.batch_size)
    val_batch_sampler = RandomBatchSampler(dataset.val_graphs, batch_size=config.hyperparams.batch_size)
    test_batch_sampler = RandomBatchSampler(dataset.test_graphs, batch_size=config.hyperparams.batch_size)
    train_length = len(list(train_batch_sampler))
    val_length = len(list(val_batch_sampler))
    test_length = len(list(test_batch_sampler))
    node_size, edge_size = 1200, 5000
    train_graph_dataset = MultiHomoGraphDataset(dataset, config.hyperparams.batch_size, node_size=node_size,
                                                edge_size=edge_size, length=train_length)
    val_graph_dataset = MultiHomoGraphDataset(dataset, config.hyperparams.batch_size, node_size=node_size,
                                              edge_size=edge_size, length=val_length)
    test_graph_dataset = MultiHomoGraphDataset(dataset, config.hyperparams.batch_size, node_size=node_size,
                                               edge_size=edge_size, length=test_length)
    """
    # data Pre-process (mindspore doesn't need)
    # https://www.mindspore.cn/docs/zh-CN/r2.0/note/api_mapping/pytorch_diff/DataLoader.html?&highlight=dataloader
    
    # config = get_config_from_json("./configs/" + config.dataset_name + ".json")
    basis = config.basis
    epsilon = config.epsilon
    power = config.power
    identity = config.get('identity', 1)

    # add self loop. We add self loop for each graph here since the function "add_self_loop" does not
    # support batch graph.
    # trans_start = time.time()
    for i in tqdm(range(len(dataset)), desc="Pre-process"):
        g = dataset.graph_lists[i]
        g = dgl.remove_self_loop(g)
        g = dgl.add_self_loop(g)
        dataset.graph_lists[i] = basis_transform(g, basis=basis, epsilon=epsilon, power=power, identity=identity)

    # total_time = time.time() - trans_start
    # print('Basis transformation total and avg time:', total_time, total_time / len(dataset))
    print("Basis total: {}".format(dataset.graph_lists[0].edata['bases'].shape[1]))
    # exit(0)
    """

    ms.set_context(device_target='GPU', save_graphs=True, save_graphs_path="./computational_graph/",
                   mode=context.GRAPH_MODE, enable_graph_kernel=True, device_id=0)
    # device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    for fold in range(config.num_folds):
        # train_loader, val_loader = TUDataLoader(
        #     dataset, batch_size=config.hyperparams.batch_size, device=device,
        #     seed=config.seed, shuffle=True,
        #     split_name='fold10', fold_idx=fold).train_valid_loader()
        train_dataloader = ds.GeneratorDataset(train_graph_dataset, ['batched_label', 'batched_node_feat', 'row', 'col',
                                                                     'node_count', 'edge_count', 'node_map_idx',
                                                                     'edge_map_idx', 'graph_mask'],
                                               sampler=train_batch_sampler, python_multiprocessing=True)
        val_dataloader = ds.GeneratorDataset(val_graph_dataset, ['batched_label', 'batched_node_feat', 'row', 'col',
                                                                 'node_count', 'edge_count', 'node_map_idx',
                                                                 'edge_map_idx', 'graph_mask'],
                                             sampler=val_batch_sampler, python_multiprocessing=True)
        test_dataloader = ds.GeneratorDataset(test_graph_dataset, ['batched_label', 'batched_node_feat', 'row', 'col',
                                                                   'node_count', 'edge_count', 'node_map_idx',
                                                                   'edge_map_idx', 'graph_mask'],
                                              sampler=test_batch_sampler, python_multiprocessing=True)

        print('-------- FOLD' + str(fold) +
              ' DATASET=' + config.dataset_name +
              ', COMMIT_ID=' + config.commit_id)

        test_accs, train_losses, train_accs = run_given_fold(
            dataset.node_feat_size,
            dataset.label_dim,
            # dataset.graph_lists[0].edata['bases'].shape[1],
            dataset.graph_edges.size,
            train_dataloader,
            val_dataloader,
            ts_kf_algo_hp=str(config.time_stamp) + '/f' + str(fold) + '/' + algo_hp,
            config=config
        )

        folds_test_accs.append(np.array(test_accs))
        folds_train_losses.append(np.array(train_losses))
        folds_train_accs.append(np.array(train_accs))

        avg_test_accs, std_test_accs = k_folds_average(folds_test_accs)
        sel_epoch = np.argmax(avg_test_accs)
        sel_test_acc = np.max(avg_test_accs)
        sel_test_acc_std = std_test_accs[sel_epoch]
        sel_test_with_std = str(sel_test_acc) + '_' + str(sel_test_acc_std)

        avg_train_losses, std_train_losses = k_folds_average(folds_train_losses)
        sel_tl_with_std = str(np.min(avg_train_losses)) + '_' + str(std_train_losses[np.argmin(avg_train_losses)])

        avg_train_accs, std_train_accs = k_folds_average(folds_train_accs)
        sel_ta_with_std = str(np.max(avg_train_accs)) + '_' + str(std_train_accs[np.argmax(avg_train_accs)])

        print('--------')
        print('Best Test Acc:   ' + sel_test_with_std + ', Epoch: ' + str(sel_epoch))
        print('Best Train Loss: ' + sel_tl_with_std)
        print('Best Train Acc:  ' + sel_ta_with_std)

        print('FOLD' + str(fold + 1) + ', '
              + config.dataset_name + ', '
              + str(config.time_stamp) + '/'
              + algo_hp
              + ', BT=' + sel_test_with_std
              + ', BE=' + str(sel_epoch)
              + ', ID=' + config.commit_id)

    if config.get('num_folds', 10) > 1:
        ts_fk_algo_hp = str(config.time_stamp) + '/fk' + str(config.get('num_folds', 10)) + '/' + algo_hp
        for i in range(1, config.hyperparams.epochs):
            test_acc = avg_test_accs[i - 1]
            train_loss = avg_train_losses[i - 1]
            train_acc = avg_train_accs[i - 1]

            # writer.add_scalars('traP', {ts_fk_algo_hp: train_acc}, i)
            # # writer.add_scalars('valP', {ts_fk_algo_hp: valid_acc}, i)
            # writer.add_scalars('tstP', {ts_fk_algo_hp: test_acc}, i)
            # writer.add_scalars('traL', {ts_fk_algo_hp: train_loss}, i)
            # # writer.add_scalars('lr',   {ts_kf_algo_hp: lr}, i)


def main():
    args = get_args()
    config = process_config(args)
    print(config)

    for seed in config.seeds:
        config.seed = seed
        config.time_stamp = int(time.time())
        print(config)
        config.architecture.dropout = float(config.architecture.dropout)
        run_model(config)


if __name__ == "__main__":
    main()


'''
backup for translate from torch to mindspore

torch.optim.Adam	mindspore.nn.Adam
-

torch.optim.lr_scheduler.StepLR	mindspore.nn.piecewise_constant_lr
https://www.mindspore.cn/docs/zh-CN/r2.0.0-alpha/note/api_mapping/pytorch_diff/PiecewiseConstantLR.html

'''