import torch
import torch.nn as nn
import torch.nn.functional as F

from distributions import Categorical, Categorical2D
from utils import init, init_normc_
import math

import numpy as np

from densenet_pytorch.densenet import DenseNet
# from coord_conv_pytorch.coord_conv import nn.Conv2d, nn.Conv2dTranspose
#from nn.Conv2d_pytorch.nn.Conv2d import nn.Conv2d, nn.Conv2dTranspose
from ConvLSTMCell import ConvLSTMCell

class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class Policy(nn.Module):
    def __init__(self, obs_shape, action_space, base_kwargs=None, curiosity=False, algo='A2C', model='MicropolisBase', args=None):
        super(Policy, self).__init__()
        self.curiosity = curiosity
        self.args = args
        if base_kwargs is None:
            base_kwargs = {}

        if len(obs_shape) == 3:
            if curiosity:
                self.base = MicropolisBase_ICM(obs_shape[0], **base_kwargs)
            else: 
                if not args.model:
                    args.model = 'fixed'
                base_model = globals()['MicropolisBase_{}'.format(args.model)]
                if args.model == 'fractal':
                    base_kwargs = {**base_kwargs, **{'n_recs': args.n_recs, 'n_conv_recs': args.n_conv_recs, 'squeeze':args.squeeze, 'shared':args.shared}}
                self.base = base_model(obs_shape[0], **base_kwargs)
            print('BASE NETWORK: \n', self.base)

        elif len(obs_shape) == 1:
            self.base = MLPBase(obs_shape[0], **base_kwargs)
        else:
            raise NotImplementedError

        if action_space.__class__.__name__ == "Discrete":
            if True:
                num_outputs = action_space.n
                self.dist = Categorical2D(self.base.output_size, num_outputs)
            else:
                num_outputs = action_space.n
                self.dist = Categorical2D(self.base.output_size, num_outputs)

        elif action_space.__class__.__name__ == "Box":
            num_outputs = action_space.shape[0]
            if self.args.env_name == 'MicropolisPaintEnv-v0':
                self.dist = None
            else:
    #           self.dist = DiagGaussian(self.base.output_size, num_outputs)
                self.dist = Categorical2D(self.base.output_size, num_outputs)

        else:
            raise NotImplementedError



    @property
    def is_recurrent(self):
        return self.base.is_recurrent

    @property
    def recurrent_hidden_state_size(self):
        """Size of rnn_hx."""
        return self.base.recurrent_hidden_state_size

    def forward(self, inputs, rnn_hxs, masks):
        raise NotImplementedError

    def act(self, inputs, rnn_hxs, masks, deterministic=False,
            player_act=None, icm_enabled=False):
        ''' assumes player actions can only occur on env rank 0'''
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        action_bin = None
        if 'paint' in self.args.env_name.lower():
            dist = torch.distributions.binomial.Binomial(1, actor_features)
            action = dist.sample()
            action_log_probs = dist.log_prob(action)


        else:
            dist = self.dist(actor_features)
            if player_act:
                # force the model to sample the player-selected action
                play_features = actor_features
                play_features = play_features.view(actor_features.size(0), -1)
                play_features.fill_(-99999)
                play_features[:1, player_act] = 99999
                play_features = play_features.view(actor_features.shape)
                play_dist = self.dist(play_features)
                action = play_dist.sample()
                # backprop is sent through the original distribution
                action_log_probs = dist.log_probs(action)

            else:

                if deterministic:
                    action = dist.mode()
                else:
                    action = dist.sample()

                action_log_probs = dist.log_probs(action)


            if icm_enabled:
                action_bin = torch.zeros(dist.probs.shape)
                action_ixs = torch.Tensor(list(range(dist.probs.size(0)))).unsqueeze(1).long()

                action_i = torch.cat((action_ixs.cuda(), action.cuda()), 1)
                action_bin[action_i[:,0], action_i[:,1]] = 1
                if torch.cuda.current_device() > 0:
                    action_bin = action_bin.cuda()



        return value, action, action_log_probs, rnn_hxs

    def icm_act(self, inputs):
        s1, pred_s1, pred_a = self.base(inputs, None, None, icm=True)
        return s1, pred_s1, self.dist(pred_a).probs

    def get_value(self, inputs, rnn_hxs, masks):
        value, _, _ = self.base(inputs, rnn_hxs, masks)
        return value

    def evaluate_icm(self, inputs):
        s1, pred_s1, pred_a = self.base(inputs, None, None, icm=True)
        return s1, pred_s1, self.dist(pred_a).probs

    def evaluate_actions(self, inputs, rnn_hxs, masks, action):

        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)
        if 'paint' in self.args.env_name.lower():
            dist = torch.distributions.binomial.Binomial(1, actor_features)
            action_log_probs = dist.log_prob(action)
            dist_entropy = None
           #dist_entropy = (dist.logits * dist.probs).mean()
        else:
            dist = self.dist(actor_features)

            action_log_probs = dist.log_probs(action)
            dist_entropy = dist.entropy().mean()
        return value, action_log_probs, dist_entropy, rnn_hxs


class NNBase(nn.Module):

    def __init__(self, recurrent, recurrent_input_size, hidden_size):
        super(NNBase, self).__init__()

        self._hidden_size = hidden_size
        self._recurrent = recurrent

        if recurrent:
            self.gru = nn.GRUCell(recurrent_input_size, hidden_size)
            nn.init.orthogonal_(self.gru.weight_ih.data)
            nn.init.orthogonal_(self.gru.weight_hh.data)
            self.gru.bias_ih.data.fill_(0)
            self.gru.bias_hh.data.fill_(0)

    @property
    def is_recurrent(self):
        return self._recurrent

    @property
    def recurrent_hidden_state_size(self):
        if self._recurrent:
            return self._hidden_size
        return 1

    @property
    def output_size(self):
        return self._hidden_size

    def _forward_gru(self, x, hxs, masks):
        if x.size(0) == hxs.size(0):
            x = hxs = self.gru(x, hxs * masks)
        else:
            # x is a (T, N, -1) tensor that has been flatten to (T * N, -1)
            N = hxs.size(0)
            T = int(x.size(0) / N)

            # unflatten
            x = x.view(T, N, x.size(1))

            # Same deal with masks
            masks = masks.view(T, N, 1)

            outputs = []
            for i in range(T):
                hx = hxs = self.gru(x[i], hxs * masks[i])
                outputs.append(hx)

            # assert len(outputs) == T
            # x is a (T, N, -1) tensor
            x = torch.stack(outputs, dim=0)
            # flatten
            x = x.view(T * N, -1)

        return x, hxs

class MicropolisBase_FullyConv(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=256, 
            map_width=20, num_actions=18):

        super(MicropolisBase_FullyConv, self).__init__(recurrent, hidden_size, hidden_size)
        num_actions = num_actions
        self.map_width = map_width
        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0.1),
            nn.init.calculate_gain('relu'))

        self.embed = init_(nn.Conv2d(num_inputs, 32, 1, 1, 0))
        self.k5 = init_(nn.Conv2d(32, 16, 5, 1, 2))
        self.k3 = init_(nn.Conv2d(16, 32, 3, 1, 1))
        state_size = map_width * map_width * 32

        init_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0))

        self.dense = init_(nn.Linear(state_size, 256))
        self.val = init_(nn.Linear(256, 1))

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.act = init_(nn.Conv2d(32, num_actions, 1, 1, 0))

    def forward(self, x, rhxs, masks):

        x = F.relu(self.embed(x))
        x = F.relu(self.k5(x))
        x = F.relu(self.k3(x))
        x_lin = torch.tanh(self.dense(x.view(x.shape[0], -1)))
        val = self.val(x_lin)
        act = self.act(x)

        return val, act, rhxs

class MicropolisBase_fractal(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512,
                 map_width=20, n_conv_recs=2, n_recs=30, squeeze=False,
                 shared=False,
                 num_actions=19):

        super(MicropolisBase_fractal, self).__init__(
                recurrent, hidden_size, hidden_size)

        self.map_width = map_width
        self.n_channels = 32
        self.n_recs = n_recs
        print("Fractal Net: ", self.n_recs, "recursions, ",squeeze, "squeeze")
        self.n_conv_recs = n_conv_recs
        self.squeeze = squeeze

        self.SKIPSQUEEZE = True # actually we mean a fractal rule that grows linearly in max depth but exponentially in number of columns, rather than vice versa, with number of recursions #TODO: combine the two rules
        if self.SKIPSQUEEZE:
            self.n_cols = 2 ** (self.n_recs - 1)
        else:
            self.n_cols = self.n_recs
        self.COLUMNS = False # if true, we do not construct the network recursively, but as a row of concurrent columns
        self.join_masks = self.init_join_masks()
        print('join masks: {}'.format(self.join_masks))
        
        self.SHARED = shared # if true, weights are shared between recursions
        self.local_drop = False
        # at each join, which columns are taken as input (local drop as described in Fractal Net paper)
        self.global_drop = False
        self.active_column = None

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0.1),
            nn.init.calculate_gain('relu'))

        self.conv_00 = init_(nn.Conv2d(num_inputs, self.n_channels, 1, 1, 0))
        if self.SHARED:
            n_unique_lyrs = 1
        else:
            n_unique_lyrs = self.n_recs
        for i in range(n_unique_lyrs):
            setattr(self, 'fixed_{}'.format(i), init_(nn.Conv2d(
                self.n_channels, self.n_channels, 3, 1, 1)))
            if n_unique_lyrs == 1 or i > 0:
                setattr(self, 'join_{}'.format(i), init_(nn.Conv2d(
                    self.n_channels * 2, self.n_channels, 3, 1, 1)))
                if squeeze or self.SKIPSQUEEZE:
                    setattr(self, 'dwn_{}'.format(i), init_(nn.Conv2d(
                        self.n_channels, self.n_channels, 2, 2, 0)))
                    setattr(self, 'up_{}'.format(i), init_(nn.ConvTranspose2d(
                        self.n_channels, self.n_channels, 2, 2, 0)))


        f_c = None
        if not self.COLUMNS:
            for i in range(self.n_recs):
                if self.SKIPSQUEEZE:
                    f_c = SkipFractal(self, f_c, n_rec=i)
                elif squeeze:
                    f_c = SubFractal_squeeze(self, f_c, n_layer=i, map_width=self.map_width)
                else:
                    f_c = SubFractal(self, f_c, n_layer=i)

        self.f_c = f_c
        self.compress = init_(nn.Conv2d(2 * self.n_channels, self.n_channels, 3, 1, 1))

        self.critic_squeeze = init_(nn.Conv2d(self.n_channels, self.n_channels, 2, 2, 0))

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))


        self.actor_out = init_(nn.Conv2d(self.n_channels, num_actions, 3, 1, 1)) 
        self.critic_out = init_(nn.Conv2d(self.n_channels, 1, 3, 1, 1))

    def forward(self, x, rnn_hxs, masks):

        depth = pow(2, self.n_recs - 1)
        x = x_0 = F.relu(self.conv_00(x))
        if not self.COLUMNS:
            # (column, join depth)
            if self.SKIPSQUEEZE:
                net_coords = (0, self.n_recs - 1)
            else:
                net_coords = (self.n_recs - 1, depth - 1 )
            x = F.relu(self.f_c(x, net_coords))
        else:
            if self.global_drop:
                j = self.active_column
                for i in range(depth // (2 ** j)):
                   #active_column = getattr(self, 'col_{}'.format(j))
                   #x = active_column(x)
                    fixed = getattr(self, 'fixed_{}'.format(j))
                    if self.squeeze:
                        if j > 0:
                            dwn = getattr(self, 'dwn_{}'.format(j))
                            up = getattr(self, 'up_{}'.format(j))
                        for k in range(j):
                            x = F.relu(dwn(x))
                        x = F.relu(fixed(x))
                        for k in range(j):
                            x = F.relu(up(x))
                    else:
                        x = F.relu(fixed(x))
            else:
                for r in range(self.n_recs):
                    globals()['in_{}'.format(r)] = x
                for i in range(depth):
                   #print('depth ', i)
                    j = 0 # potential inputs added to join layer
                    a = 0 # actual added inputs to join layer
                    out = None
                    while (i + 1) % (2 ** j) == 0 and j < self.n_recs:
                        # check for local path drop
                        if not self.local_drop or self.join_masks[i][j]:
                           #column = getattr(self, 'col_{}'.format(j))
                            out_j = globals()['in_{}'.format(j)]
                            fixed = getattr(self, 'fixed_{}'.format(j))
                            if j > 0:
                                join = getattr(self, 'join_{}'.format(j))
                            if self.squeeze and j > 0:
                                dwn = getattr(self, 'dwn_{}'.format(j))
                                up = getattr(self, 'up_{}'.format(j))
                                for k in range(j):
                                    out_j = F.relu(dwn(out_j))
                                out_j = F.relu(fixed(out_j))
                                for k in range(j):
                                    out_j = F.relu(up(out_j))
                            else:
                                out_j = F.relu(fixed(out_j)) 
                           #out_j = column(globals()['in_{}'.format(j)])
                           #out = (out * a + out_j) / (a + 1)
                            if out is None:
                                out = out_j
                            else:
                                out = F.relu(join(torch.cat((out, out_j), 1)))
                            a += 1
                       #print('depth {} joined w/ col {}'.format(i,j))
                        j += 1
                    for k in range(j):
                        globals()['in_{}'.format(k)] = out 
                x = in_0


        x = F.relu(self.compress(torch.cat((x, x_0), 1)))
        values = x
        for i in range(int(math.log(self.map_width, 2))):
            values = F.relu(self.critic_squeeze(values))
        values = self.critic_out(values)
        values = values.view(values.size(0), -1)
        actions = self.actor_out(x)

        return values, actions, rnn_hxs


    def get_local_drop(self):
        self.global_drop = False
        self.active_column = None
        if self.SKIPSQUEEZE:
            self.local_drop = False
            self.clear_join_masks
            return
        self.local_drop = True
        i = 0
        for mask in self.join_masks:
            n_ins = len(mask)
            mask = np.random.random_sample(n_ins) > 0.15
            if 1 not in mask:
                mask[np.random.randint(0, n_ins)] = 1
            self.join_masks[i] = mask
            i += 1

    def init_join_masks(self):
        ''' Initializes a data structure which indicates which subnetwork of
        the fractal should be active on a particular forward pass. '''
        if self.SKIPSQUEEZE:
            depth = self.n_recs
            max_width = 2 ** (self.n_recs - 1)
            join_masks = np.ones((depth, max_width))
            return join_masks
        join_masks = []
        max_depth =  2 ** (self.n_recs - 1)
        max_width = self.n_recs
        for i in range(max_depth):
            n = 1
            width_i = 0
            while (i + 1) % n == 0:
                n = n * 2
                width_i += 1
            mask = [1]*width_i
            self.join_masks.append(mask)


    def clear_join_masks(self):
        ''' Returns a set of join masks that will result in activation flowing
        through the entire fractal network.'''
        if self.SKIPSQUEEZE:
            self.join_masks.fill(1)
            return
        i = 0
        for mask in self.join_masks:
            n_ins = len(mask)
            mask = [1]*n_ins
            self.join_masks[i] = mask
            i += 1

    def set_active_column(self, a):
        ''' Returns a set of join masks that will result in activation flowing 
        through a single sequential 'column' of the network.'''
        if a == -1:
            self.clear_join_masks()
            return
        self.active_column = a
        ### For Skip-Squeeze fractal net.
        if self.SKIPSQUEEZE:
            self.join_masks.fill(0)
            b = a # intermediary columns visited before arriving at target column
            for i in range(self.n_recs):
                skip_range = 2 ** (i)
                skip_dist = b % skip_range
                b = b - skip_dist
                if skip_dist == 0:
                    self.join_masks[i][b] = 1
           #print('active column: {}\n{}'.format(self.active_column, self.join_masks))
            return
        ### For traditional Fractal Net
        i = 0
        for mask in self.join_masks:
            n_ins = len(mask)
            mask = [0]*n_ins
            if n_ins > a:
                mask[a] = 1
            self.join_masks[i] = mask
            i += 1

    def get_global_drop(self):
        self.global_drop = True
        self.local_drop = False
        a = np.random.randint(0, self.n_recs)
        self.set_active_column(a)

    def get_drop_path(self):
        if np.random.randint(0, 2) == 1:
            self.local_drop = self.get_local_drop()
        else:
            self.global_drop = self.get_global_drop()


class SkipFractal(nn.Module):
    ''' Like fractal net, but where the longer columns compress more,
    and the shallowest column not at all.
    -skip_body - layer or sequence of layers, to be passed through Relu here'''
    def __init__(self, parent, body, n_rec, skip_body=None):
        super(SkipFractal, self).__init__()
        self.n_rec = n_rec
        self.n_channels = 32
        self.body = body
        self.active_column = parent.active_column
        self.join_masks = parent.join_masks
        self.global_drop = parent.global_drop
        if parent.SHARED:
            j = 0 # default index for shared layers
        else:
            j = n_rec # layer index = recursion index
        if n_rec == 0:
            self.fixed = getattr(parent, 'fixed_{}'.format(j))
        else:
            self.skip = skip_body
        if n_rec > 0:
            self.join = getattr(parent, 'join_{}'.format(j))
            self.up = getattr(parent, 'up_{}'.format(j))
            self.dwn = getattr(parent, 'dwn_{}'.format(j))

    def forward(self, x, net_coords=None):
        if x is None:
            return None
        col = net_coords[0]
        depth = net_coords[1]
        x_b, x_a = x, x
        if self.n_rec > 0:
            x_a = self.body(x_a, (col + 2 ** (self.n_rec - 1), depth - 1))
        else:
            x_a = None
        if self.join_masks[depth][col]:
           #print(net_coords)
            if self.n_rec > 0:
                x_b = F.relu(self.dwn(x_b))
                x_b = self.body(x_b, (col, depth - 1))
                x_b = F.relu(self.up(x_b))
            else:
                x_b = self.fixed(x)
                return x_b
        else:
            x_b = None
        if x_a is None:
            return x_b
        if x_b is None:
            return x_a
        x = F.relu(self.join(torch.cat((x_a, x_b), dim=1)))
        return x


class SubFractal(nn.Module):
    def __init__(self, root, f_c, n_layer, conv=None, n_conv_recs=2):
        super(SubFractal, self).__init__()
        self.n_layer = n_layer
        self.n_conv_recs = n_conv_recs
        self.n_channels = 32
        self.f_c = f_c
        self.active_column = root.active_column
        self.join_masks = root.join_masks
        if root.SHARED:
            j = 0
        else:
            j = n_layer
        self.fixed = getattr(root, 'fixed_{}'.format(j))
        if n_layer > 0:
            self.join = getattr(root, 'join_{}'.format(j))

    def forward(self, x, net_coords=None):
        if x is None: return None
        x_c, x_c1 = x, x
        col = net_coords[0]
        depth = net_coords[1]

       #print(self.active_column, col)
        if self.join_masks[depth][col]:
            for i in range(1):
                x_c1 = F.relu(self.fixed(x_c1))
        if col == 0:
            return x_c1
        x_c = self.f_c(x_c, (col - 1, depth - (2 ** (col - 1))))
        x_c =self.f_c(x_c, (col - 1, depth))
        if x_c1 is None:
            return x_c
        if x_c is None:
            return x_c1
        x = F.relu(self.join(torch.cat((x_c, x_c1), dim=1)))
       #x = (x_c1 + x_c * (self.n_layer)) / (self.n_layer + 1)
        return x



class SubFractal_squeeze(nn.Module):
    def __init__(self, root, f_c, n_layer, map_width=16, net_coords=None):
        super(SubFractal_squeeze, self).__init__()
        self.map_width = map_width
        self.n_layer = n_layer
        self.n_channels = 32
        self.join_masks = root.join_masks
        self.active_column = root.active_column
        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0.1),
            nn.init.calculate_gain('relu'))

        if root.SHARED:
            j = 0
        else:
            j = n_layer
        if n_layer > 0:
            self.up = getattr(root, 'up_{}'.format(j))
            self.dwn = getattr(root, 'dwn_{}'.format(j))
            self.join = getattr(root, 'join_{}'.format(j))
        self.fixed = getattr(root, 'fixed_{}'.format(j))
        self.num_down = min(int(math.log(self.map_width, 2)) - 1, n_layer)

        self.f_c = f_c

    def forward(self, x, net_coords):
        if x is None: 
            return x
        x_c, x_c1 = x, x
        col = net_coords[0]
        depth = net_coords[1]
        if self.n_layer > 0:
            x_c = self.f_c(x_c, (col - 1, depth - 2 ** (col - 1 )))
            x_c = self.f_c(x_c, (col - 1, depth))
        if self.join_masks[depth][col]:
            for d in range(self.num_down):
                x_c1 = F.relu(self.dwn(x_c1))
            for f in range(1):
                x_c1 = F.relu(self.fixed(x_c1))
            for u in range(self.num_down):
                x_c1 = F.relu(self.up(x_c1))
        if x_c is None or col == 0:
            return x_c1
        if x_c1 is None:
            return x_c
        x = F.relu(self.join(torch.cat((x_c, x_c1), dim=1)))
        #x = (x_c1 + x_c * self.n_layer) / (self.n_layer + 1)
        return x



class MicropolisBase_fixed(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512, map_width=20, num_actions=18):
        super(MicropolisBase_fixed, self).__init__(recurrent, hidden_size, hidden_size)

        self.num_recursions = map_width

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0.1),
            nn.init.calculate_gain('relu'))

        self.skip_compress = init_(nn.Conv2d(num_inputs, 15, 1, stride=1))

        self.conv_0 = init_(nn.Conv2d(num_inputs, 64, 1, 1, 0))
       #self.conv_1 = init_(nn.Conv2d(64, 64, 5, 1, 2))
        for i in range(1):
            setattr(self, 'conv_2_{}'.format(i), init_(nn.Conv2d(64, 64, 3, 1, 1)))
        self.critic_compress = init_(nn.Conv2d(79, 64, 3, 1, 1))
        for i in range(1):
            setattr(self, 'critic_downsize_{}'.format(i), init_(nn.Conv2d(64, 64, 2, 2, 0)))


        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.actor_compress = init_(nn.Conv2d(79, 19, 3, 1, 1))
        self.critic_conv = init_(nn.Conv2d(64, 1, 1, 1, 0))
        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs
        x = F.relu(self.conv_0(x))
        skip_input = F.relu(self.skip_compress(inputs))
       #x = F.relu(self.conv_1(x))
        for i in range(self.num_recursions):
            conv_2 = getattr(self, 'conv_2_{}'.format(0))
            x = F.relu(conv_2(x))
        x = torch.cat((x, skip_input), 1)
        values = F.relu(self.critic_compress(x))
        for i in range(4):
            critic_downsize = getattr(self, 'critic_downsize_{}'.format(0))
            values = F.relu(critic_downsize(values))
        values = self.critic_conv(values)
        values = values.view(values.size(0), -1)
        actions = self.actor_compress(x)

        return values, actions, rnn_hxs

class MicropolisBase_squeeze(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512, map_width=20):
        super(MicropolisBase_squeeze, self).__init__(recurrent, hidden_size, hidden_size)
        self.chunk_size = 2 # factor by which map dimensions are shrunk
        self.map_width = map_width
       #self.num_maps = 4
        self.num_maps = int(math.log(self.map_width, self.chunk_size)) - 1 # how many different sizes

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, -10),
            nn.init.calculate_gain('relu'))
        linit_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0))

        self.cmp_in = init_(nn.Conv2d(num_inputs, 64, 1, stride=1, padding=0))
        for i in range(self.num_maps):
            setattr(self, 'prj_life_obs_{}'.format(i), init_(nn.Conv2d(64, 64, 3, stride=1, padding=1)))
            setattr(self, 'cmp_life_obs_{}'.format(i), init_(nn.Conv2d(128, 64, 3, stride=1, padding=1)))
       #self.shrink_life = init_(nn.Conv2d(64, 64, 3, 3, 0))

       #self.conv_1 = init_(nn.Conv2d(64, 64, 3, 1, 1))
       #self.lin_0 = linit_(nn.Linear(1024, 1024))

        for i in range(self.num_maps):
            if i == 0:
                setattr(self, 'dwn_{}'.format(i), init_(nn.Conv2d(64, 64, 2, stride=2, padding=0)))
            setattr(self, 'expand_life_{}'.format(i), init_(nn.ConvTranspose2d(64 + 64, 64, 2, stride=2, padding=0)))
            setattr(self, 'prj_life_act_{}'.format(i), init_(nn.Conv2d(64, 64, 3, stride=1, padding=1)))
            setattr(self, 'cmp_life_act_{}'.format(i), init_(nn.Conv2d(128, 64, 3, stride=1, padding=1)))
            setattr(self, 'cmp_life_val_in_{}'.format(i), init_(nn.Conv2d(128, 64, 3, stride=1, padding=1)))
            setattr(self, 'dwn_val_{}'.format(i), init_(nn.Conv2d(64, 64, 2, stride=2, padding=0)))
            if i == self.num_maps - 1:
                setattr(self, 'prj_life_val_{}'.format(i), init_(nn.Conv2d(64, 64, 3, stride=1, padding=1)))
            else:
                setattr(self, 'prj_life_val_{}'.format(i), init_(nn.Conv2d(64, 64, 3, stride=1, padding=1)))

        self.cmp_act = init_(nn.Conv2d(128, 64, 3, stride=1, padding=1))


        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0.1))

        self.act_tomap = init_(nn.Conv2d(64, 19, 5, stride=1, padding=2))
        self.cmp_val_out = init_(nn.Conv2d(64, 1, 1, stride=1, padding=0))
        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs
        x = F.relu(self.cmp_in(x))
        x_obs = [] 
        for i in range(self.num_maps): # shrink if not first, then run life sim
            if i != 0:
                shrink_life = getattr(self, 'dwn_{}'.format(0))
                x = F.relu(shrink_life(x))
            x_0 = x
            if i > -1:
                prj_life_obs = getattr(self, 'prj_life_obs_{}'.format(i))
                for c in range(3):
                    x = F.relu(prj_life_obs(x))
                x = torch.cat((x, x_0), 1)
                cmp_life_obs = getattr(self,'cmp_life_obs_{}'.format(i))
                x = F.relu(cmp_life_obs(x))
            x_obs += [x]


        for j in range(self.num_maps): # run life sim, then expand if not last
            if j != 0:
                x_i = x_obs[self.num_maps-1-j]
                x = torch.cat((x, x_i), 1)
                cmp_life_act = getattr(self, 'cmp_life_act_{}'.format(j))
                x =  F.relu(cmp_life_act(x))
            x_0 = x
            if j > -1:
                prj_life_act = getattr(self, 'prj_life_act_{}'.format(j))
                for c in range(1):
                    x = F.relu(prj_life_act(x))
                x = torch.cat((x, x_0), 1)
            if j < self.num_maps - 1:
                expand_life = getattr(self, 'expand_life_{}'.format(j))
                x = F.relu(expand_life(x))
        x = F.relu(self.cmp_act(x))
        acts = F.relu(self.act_tomap(x))

        for i in range(self.num_maps):
            dwn_val = getattr(self, 'dwn_val_{}'.format(0))
            prj_life_val = getattr(self, 'prj_life_val_{}'.format(i))
            cmp_life_val_in = getattr(self, 'cmp_life_val_in_{}'.format(i))
            x_i = x_obs[i]
            x = torch.cat((x, x_i), 1)
            x = F.relu(cmp_life_val_in(x))
            x = F.relu(dwn_val(x))
            x = F.relu(prj_life_val(x))
        vals = self.cmp_val_out(x) 
        vals = vals.view(vals.size(0), -1)
        return  vals, acts, rnn_hxs

class MicropolisBase_ICM(MicropolisBase_fixed):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512):
        super(MicropolisBase_ICM, self).__init__(num_inputs, recurrent, hidden_size)

        ### ICM feature encoder

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))

        num_skip_inputs=15
        self.num_action_channels=19

        self.icm_state_in = init_(nn.Conv2d(num_inputs, 64, 3, 1, 1))
        self.icm_state_conv_0 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_state_out = init_(nn.Conv2d(64, 64, 3, 1, 1))

        self.icm_pred_a_in = init_(nn.Conv2d((num_inputs) * 2, 128, 3, 1, 1))
        self.icm_pred_a_conv_0 = init_(nn.Conv2d(128, 128, 3, 1, 1))

        self.icm_pred_s_in = init_(nn.Conv2d((num_inputs) + self.num_action_channels, 64, 1, 1, 0))
        self.icm_pred_s_conv_0 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_1 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_2 = init_(nn.Conv2d(64, 64, 3, 1, 1))      
        self.icm_pred_s_conv_3 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_4 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_5 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_6 = init_(nn.Conv2d(64, 64, 3, 1, 1))      
        self.icm_pred_s_conv_7 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_8 = init_(nn.Conv2d(64, 64, 3, 1, 1))
        self.icm_pred_s_conv_9 = init_(nn.Conv2d(64, 64, 3, 1, 1))      
        self.icm_pred_s_conv_10 = init_(nn.Conv2d(64, 64, 3, 1, 1))

       #self.icm_skip_compress = init_(nn.Conv2d(num_inputs, 15, 1, stride=1))



        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.icm_pred_a_out = init_(nn.Conv2d(128, self.num_action_channels, 7, 1, 3))
        self.icm_pred_s_out = init_(nn.Conv2d(64 + 64, num_inputs, 1, 1, 0))

        self.train()

    def forward(self, inputs, rnn_hxs, masks, icm=False):
        if icm == False:
            return super().forward(inputs, rnn_hxs, masks)
            
        else:

            # Encode state feature-maps

            s0_in, s1_in, a1 = inputs
            a1 = a1.view(a1.size(0), self.num_action_channels, 20, 20)

            s0 = s0_in
          # s0 = F.relu(self.icm_state_in(s0))
          # for i in range(1):
          #     s0 = F.relu(self.icm_state_conv_0(s0))
          # s0 = F.relu(self.icm_state_out(s0))
          ##s0_skip = F.relu(self.icm_skip_compress(s0))

            s1 = s1_in
          # s1 = F.relu(self.icm_state_in(s1))
          # for i in range(1):
          #     s1 = F.relu(self.icm_state_conv_0(s1))
          # s1 = F.relu(self.icm_state_out(s1))
          ##s1_skip = F.relu(self.icm_skip_compress(s1_in))

            # Predict outcome state feature-map and action dist.
            if True:
                a1 = a1.cuda()
                s0 = s0.cuda()

            pred_s1 = pred_s1_0 = F.relu(self.icm_pred_s_in(torch.cat((s0, a1), 1)))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_0(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_1(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_2(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_3(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_4(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_5(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_6(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_7(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_8(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_9(pred_s1))
            for i in range(2):
                pred_s1 = F.relu(self.icm_pred_s_conv_10(pred_s1))

            pred_s1 = torch.cat((pred_s1, pred_s1_0), 1)
            pred_s1 = self.icm_pred_s_out(pred_s1)

            pred_a = F.relu(self.icm_pred_a_in(torch.cat((s0, s1), 1)))
            for i in range(1):
                pred_a = F.relu(self.icm_pred_a_conv_0(pred_a))
            pred_a = self.icm_pred_a_out(pred_a)
            pred_a = pred_a.view(pred_a.size(0), -1)

            return s1, pred_s1, pred_a

    def feature_state_size(self):
        return (32, 20, 20)


class MicropolisBase_acktr(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512):
        super(MicropolisBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))

        import sys

       #self.skip_compress = init_(nn.Conv2d(num_inputs, 15, 1, stride=1))

        self.conv_0 = nn.Conv2d(num_inputs, 64, 1, 1, 0)
        init_(self.conv_0)
        self.conv_1 = nn.Conv2d(64, 64, 5, 1, 2)
        init_(self.conv_1)
       #self.conv_2 = nn.Conv2d(64, 64, 3, 1, 0)
       #init_(self.conv_2)
       #self.conv_3 = nn.ConvTranspose2d(64, 64, 3, 1, 0)
       #init_(self.conv_3)
        self.actor_compress = init_(nn.Conv2d(64, 20, 3, 1, 1))

        self.critic_compress = init_(nn.Conv2d(64, 8, 1, 1, 1))

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.critic_conv_1 = init_(nn.Conv2d(8, 1, 20, 20, 0))
        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs
        x = F.relu(self.conv_0(x))
       #skip_input = F.relu(self.skip_compress(inputs))
        x = F.relu(self.conv_1(x))
       #for i in range(5):
       #    x = F.relu(self.conv_2(x))
       #for j in range(5):
       #    x = F.relu(self.conv_3(x))
       #x = torch.cat((x, skip_input), 1)
        values = F.relu(self.critic_compress(x))
        values = self.critic_conv_1(values)
        values = values.view(values.size(0), -1)
        actions = F.relu(self.actor_compress(x))

        return values, actions, rnn_hxs


class MicropolisBase_1d(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512):
        super(MicropolisBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))

        import sys

        self.skip_compress = init_(nn.Conv2d(num_inputs, 15, 1, stride=1))

        self.conv_0 = nn.Conv2d(num_inputs, 64, 1, 1, 0)
        init_(self.conv_0)
        self.conv_1 = nn.Conv2d(64, 64, 5, 1, 2)
        init_(self.conv_1)
        self.conv_2 = nn.Conv2d(1, 1, 3, 1, 0)
        init_(self.conv_2)
        self.conv_2_chan = nn.ConvTranspose2d(1, 1, (1, 3), 1, 0)
        init_(self.conv_2_chan)
        self.conv_3 = nn.ConvTranspose2d(1, 1, 3, 1, 0)
        init_(self.conv_3)
        self.conv_3_chan = nn.Conv2d(1, 1, (1, 3), 1, 0)
        init_(self.conv_3_chan)

        self.actor_compress = init_(nn.Conv2d(79, 20, 3, 1, 1))

        self.critic_compress = init_(nn.Conv2d(79, 8, 1, 1, 1))

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.critic_conv_1 = init_(nn.Conv2d(8, 1, 20, 20, 0))
        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs
        x = F.relu(self.conv_0(x))
        skip_input = F.relu(self.skip_compress(inputs))
        x = F.relu(self.conv_1(x))
        num_batch = x.size(0)
        for i in range(5):
            w, h = x.size(2), x.size(3)
            num_chan = x.size(1)
            x = x.view(num_batch * num_chan, 1, w, h)
            x = F.relu(self.conv_2(x))
            w, h = x.size(2), x.size(3)
            x = x.view(num_batch, num_chan, w, h)
            x = x.permute(0, 2, 3, 1)
            x = x.view(num_batch, 1, w * h, num_chan)
            x = F.relu(self.conv_2_chan(x))
            num_chan = x.size(3)
            x = x.view(num_batch, num_chan, w, h)
        for j in range(5):
            w, h = x.size(2), x.size(3)
            num_chan = x.size(1)
            x = x.view(num_batch * num_chan, 1, w, h)
            x = F.relu(self.conv_3(x))
            w, h = x.size(2), x.size(3)
            x = x.view(num_batch, num_chan, w, h)
            x = x.permute(0, 2, 3, 1)
            x = x.view(num_batch, 1, w * h, num_chan)
            x = F.relu(self.conv_3_chan(x))
            num_chan = x.size(3)
            x = x.view(num_batch, num_chan, w, h)
        x = torch.cat((x, skip_input), 1)
        values = F.relu(self.critic_compress(x))
        values = self.critic_conv_1(values)
        values = values.view(values.size(0), -1)
        actions = F.relu(self.actor_compress(x))

        return values, actions, rnn_hxs


class MicropolisBase_0(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512):
        super(MicropolisBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))

        import sys
        if sys.version[0] == '2':
            num_inputs=104
      # assert num_inputs / 4 == 25

        self.conv_A_0 = nn.Conv2d(num_inputs, 64, 5, 1, 2)
        init_(self.conv_A_0)
        self.conv_B_0 = nn.Conv2d(num_inputs, 64, 3, 1, 2)
        init_(self.conv_B_0)

        self.conv_A_1 = nn.Conv2d(64, 64, 5, 1, 2)
        init_(self.conv_A_1)
        self.conv_B_1 = nn.Conv2d(64, 64, 3, 1, 1)
        init_(self.conv_B_1)


        self.input_compress = nn.Conv2d(num_inputs, 15, 1, stride=1)
        init_(self.input_compress)
        self.actor_compress = nn.Conv2d(79, 18, 3, 1, 1)
        init_(self.actor_compress)


        self.critic_compress = init_(nn.Conv2d(79, 8, 1, 1, 0))
      # self.critic_conv_0 = init_(nn.Conv2d(16, 1, 20, 1, 0))

        init_ = lambda m: init(m,
            nn.init.dirac_,
            lambda x: nn.init.constant_(x, 0))

        self.critic_conv_1 = init_(nn.Conv2d(8, 1, 20, 1, 0))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
#       inputs = torch.Tensor(inputs)
#       inputs =inputs.view((1,) + inputs.shape)
        x = inputs
        x_A = self.conv_A_0(x)
        x_A = F.relu(x_A)
        x_B = self.conv_B_0(x)
        x_B = F.relu(x_B)
        for i in range(2):
#           x = torch.cat((x, inputs[:,-26:]), 1)
            x_A = F.relu(self.conv_A_1(x_A))
        for i in range(5):
            x_B = F.relu(self.conv_B_1(x_B))
        x = torch.mul(x_A, x_B)
        skip_input = F.relu(self.input_compress(inputs))
        x = torch.cat ((x, skip_input), 1)
        values = F.relu(self.critic_compress(x))
#       values = F.relu(self.critic_conv_0(values))
        values = self.critic_conv_1(values).view(values.size(0), -1)
        actions = F.relu(self.actor_compress(x))

        return values, actions, rnn_hxs

class CNNBase(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=512):
        super(CNNBase, self).__init__(recurrent, hidden_size, hidden_size)

        init_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain('relu'))

        self.main = nn.Sequential(
            init_(nn.Conv2d(num_inputs, 32, 8, stride=4)),
            nn.ReLU(),
            init_(nn.Conv2d(32, 64, 4, stride=2)),
            nn.ReLU(),
            init_(nn.Conv2d(64, 32, 3, stride=1)),
            nn.ReLU(),
            Flatten(),
            init_(nn.Linear(32 * 7 * 7, hidden_size)),
            nn.ReLU()
        )

        init_ = lambda m: init(m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0))

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = self.main(inputs / 255.0)

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        return self.critic_linear(x), x, rnn_hxs


class MicropolisBase_mlp(NNBase):
    def __init__(self, num_inputs, recurrent=False, hidden_size=64, map_width=20, num_actions=19):
        super(MicropolisBase_mlp, self).__init__(recurrent, num_inputs, hidden_size)
        num_inputs = map_width * map_width * 32
        if recurrent:
            num_inputs = hidden_size

        init_ = lambda m: init(m,
            init_normc_,
            lambda x: nn.init.constant_(x, 0))

        self.actor = nn.Sequential(
            init_(nn.Linear(num_inputs, hidden_size)),
            nn.Tanh(),
            init_(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh(),
            init_(nn.Linear(hidden_size, map_width*map_width*num_actions)),
            nn.Tanh()
        )

        self.critic = nn.Sequential(
            init_(nn.Linear(num_inputs, hidden_size)),
            nn.Tanh(),
            init_(nn.Linear(hidden_size, hidden_size)),
            nn.Tanh()
        )

        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        x = inputs
        x=x.view(x.size(0), -1)

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(x, rnn_hxs, masks)

        hidden_critic = self.critic(x)
        hidden_actor = self.actor(x)

        return self.critic_linear(hidden_critic), hidden_actor, rnn_hxs
