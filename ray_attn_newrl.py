import argparse
import numpy as np
import os
import random

import socket
import pickle

import msgpack

import gymnasium as gym
import numpy as np
import torch
from torch import nn
import torch.optim as optim
import torch.distributions as distributions
from ray.rllib.algorithms.ppo.ppo import PPOConfig
from ray.rllib.algorithms.ppo.ppo_catalog import PPOCatalog
from ray.rllib.core.models.configs import ModelConfig, MLPHeadConfig, ActorCriticEncoderConfig
from ray.rllib.core.models.torch.base import TorchModel
from ray.rllib.core.models.base import Encoder, ENCODER_OUT
from ray.rllib.core.rl_module.rl_module import SingleAgentRLModuleSpec, RLModuleConfig
from ray.rllib.algorithms.ppo.torch.ppo_torch_rl_module import PPOTorchRLModule

import ray
from ray import air, tune
from ray.rllib.env.env_context import EnvContext
from ray.rllib.utils.framework import try_import_tf, try_import_torch
from ray.rllib.utils.test_utils import check_learning_achieved
from ray.tune.logger import pretty_print
from ray.tune.registry import get_trainable_cls
from ray.rllib.models.torch.torch_distributions import TorchDiagGaussian 

from scipy.ndimage import gaussian_filter
from scipy.interpolate import griddata
from typing import Dict

from ray.rllib import Policy
from ray.rllib.algorithms.callbacks import DefaultCallbacks
from ray.rllib.env import BaseEnv
#from ray.rllib.evaluation import MultiAgentEpisode
from ray.rllib.utils.typing import PolicyID
from ray.tune import with_resources

import time


# tf1, tf, tfv = try_import_tf()
torch, nn = try_import_torch()

parser = argparse.ArgumentParser()
parser.add_argument(
    "--run", type=str, default="PPO", help="The RLlib-registered algorithm to use."
)
parser.add_argument(
    "--framework",
    choices=["tf", "tf2", "torch"],
    default="torch",
    help="The DL framework specifier.",
)
parser.add_argument(
    "--as-test",
    action="store_true",
    help="Whether this script should be run as a test: --stop-reward must "
    "be achieved within --stop-timesteps AND --stop-iters.",
)
parser.add_argument(
    "--stop-iters", type=float, default=15000, help="Number of iterations to train."
)
parser.add_argument(
    "--stop-timesteps", type=float, default=19000000, help="Number of timesteps to train."
)
parser.add_argument(
    "--stop-reward", type=float, default= 900000, help="Reward at which we stop training."
)
parser.add_argument(
    "--no-tune",
    action="store_true",
    help="Run without Tune using a manual train loop instead. In this case,"
    "use PPO without grid search and no TensorBoard.",
)
parser.add_argument(
    "--local-mode",
    action="store_true",
    help="Init Ray in local mode for easier debugging.",
)


# obs_shape = (4, 32, 32)
obs_shape = (4, 64, 64)
nx, ny = 128, 128
x = np.zeros(obs_shape).astype(np.float16)
N = 64
x_bytes = 327685

H = 6


class CustomEncoderConfig(ModelConfig):
    
    def __init__(self, num_channels):
        self.num_channels = num_channels

    def build(self, framework):
        # Your custom encoder
        return CustomEncoder(self)


class CustomEncoder(TorchModel, Encoder):
    def __init__(self, config):
        super().__init__(config)

        self.net = nn.Sequential(
            nn.ZeroPad2d((1, 1, 1, 1)), 
            nn.Conv2d(in_channels=obs_shape[0], out_channels=obs_shape[0], kernel_size=(8, 8), stride=(2, 2)),
            nn.ZeroPad2d((1, 1, 1, 1)),
            nn.Conv2d(in_channels=obs_shape[0], out_channels=config.num_channels[0], kernel_size=(4, 4), stride=(2, 2)),
            nn.ReLU(),
            nn.ZeroPad2d((1, 2, 1, 2)),
            nn.Conv2d(in_channels=config.num_channels[0], out_channels=config.num_channels[1], kernel_size=(4, 4), stride=(2, 2)),
            nn.LeakyReLU(),
            nn.ZeroPad2d((5, 5, 5, 5)),
            nn.Conv2d(in_channels=config.num_channels[1], out_channels=config.num_channels[2], kernel_size=(11, 11), stride=(1, 1)),
            nn.Flatten(1, -1)
        )  

    def _add_ds_encoder(self):
        self.net.insert(module = nn.ZeroPad2d((1, 1, 1, 1)), index = 0)
        self.net.insert(module = nn.Conv2d(in_channels=obs_shape[0], out_channels=obs_shape[0], kernel_size=(8, 8), stride=(2, 2)), index = 1)

    def _forward(self, input_dict, **kwargs):        
        return {ENCODER_OUT: (self.net(input_dict["obs"]))}


class CNNActorHeadConfig(ModelConfig):

    def build(self, framework: str = "torch") -> "Model":

        return CNNActorHead(self)

class CNNActorHead(TorchModel):

    def __init__(self, config: CNNActorHeadConfig) -> None:
        super().__init__(config)

        self.config = config
        self.embedding_dim = 256
        self.num_heads = 4    
        
        self.actor_mu = nn.Sequential(
            nn.Linear(16384, 4096),
            nn.Linear(4096, 256), 
            nn.ReLU()
        )
        self.actor_sigma = nn.Sequential(
            nn.Linear(16384, 4096), 
            nn.ReLU(), 
            nn.Linear(4096, 256)
        )

        self.netq = nn.Sequential(nn.Linear(256, self.embedding_dim))
        self.netk = nn.Sequential(nn.Linear(256, self.embedding_dim))
        self.netv = nn.Sequential(nn.Linear(256, self.embedding_dim))

        self.mean_after_attn = nn.Linear(512, 256)
        self.sigma_after_attn = nn.Linear(512, 256)
        self.attn = nn.MultiheadAttention(self.embedding_dim, self.num_heads)


    def _add_ds_layer(self):
        self.actor_mu.append(nn.Linear(nx*ny, 256))
        self.actor_sigma.append(nn.Linear(nx*ny, 256))

    def _get_attention(self, x):

        q, k, v = self.netq(x), self.netk(x), self.netv(x)
        multihead_attn = self.attn
        attn_output, attn_output_weights = multihead_attn(q, k, v)
        return torch.cat((x, attn_output), dim = 1)    

    def _forward(self, inputs: torch.Tensor, **kwargs) -> torch.Tensor:

        out_mean = self.actor_mu(inputs)
        out_sigma = self.actor_sigma(inputs)

        mean_attn = self._get_attention(out_mean)
        sigma_attn = self._get_attention(out_sigma)

        mean = self.mean_after_attn (mean_attn)
        sigma = self.sigma_after_attn (sigma_attn)

        return torch.cat((-1*mean, -1*sigma), dim=1)


class CNNCriticHeadConfig(ModelConfig):

    def build(self, framework: str = "torch") -> "Model":

        return CNNCriticHead(self)

class CNNCriticHead(TorchModel):

    def __init__(self, config: CNNCriticHeadConfig) -> None:
        super().__init__(config)

        self.critic_net = nn.Sequential(
            nn.Linear(16384, 10)
        )

        self.embedding_dim = 10
        self.num_heads = 2

        self.netq = nn.Sequential(nn.Linear(10, self.embedding_dim))
        self.netk = nn.Sequential(nn.Linear(10, self.embedding_dim))
        self.netv = nn.Sequential(nn.Linear(10, self.embedding_dim))

        self.value_after_attn = nn.Linear(20, 1)
        self.attn = nn.MultiheadAttention(self.embedding_dim, self.num_heads)

    def _get_attention(self, x):

        q, k, v = self.netq(x), self.netk(x), self.netv(x)
        multihead_attn = self.attn
        attn_output, attn_output_weights = multihead_attn(q, k, v)
        return torch.cat((x, attn_output), dim = 1)     

            
    def _forward(self, inputs: torch.Tensor, **kwargs) -> torch.Tensor:

        out = self.critic_net(inputs)
        val_attn = self._get_attention(out)
        out_val = self.value_after_attn(val_attn)

        return out_val


class CustomTorchPPORLModule(PPOTorchRLModule):
    # def __init__(self, config: RLModuleConfig):
    #     super().__init__(config)
    
    def setup(self):

        custom_encoder_config = CustomEncoderConfig(num_channels=self.config.model_config_dict['num_channels'])
        actor_critic_encoder_config = ActorCriticEncoderConfig(base_encoder_config=custom_encoder_config, shared=self.config.model_config_dict['shared'])
        self.encoder = actor_critic_encoder_config.build(framework="torch")


        pi_config = CNNActorHeadConfig()
        vf_config = CNNCriticHeadConfig()

        self.pi = pi_config.build(framework="torch")
        self.vf = vf_config.build(framework="torch")

        #self.action_dist_cls = TorchDiagGaussian TorchDeterministic
        self.action_dist_cls =  TorchDiagGaussian

        if self.config.model_config_dict['checkpoint']:
            with open(self.config.model_config_dict['checkpoint'], 'rb') as f:
                weights = pickle.load(f)["weights"]    
                # weights = pickle.load(f)
            # weights = dict(np.load(self.config.model_config_dict['checkpoint'], allow_pickle=True))
            weights = {k: torch.Tensor(weights[k]) for k in weights.keys()}
            self.load_state_dict(weights)
            del weights

       # if self.config.model_config_dict['ds']:
       #     self.pi._add_ds_layer()

        if self.config.model_config_dict['ds']:
            self.encoder.actor_encoder._add_ds_encoder()
            self.encoder.critic_encoder._add_ds_encoder()

class FluidEnv(gym.Env):


    def __init__(self, config: EnvContext):
    
        self.time = config["time"]
        self.count = 0

        self.observation_space = gym.spaces.Box(low=-10.0, high=10.00, shape=obs_shape, dtype=np.float16)
        self.action_space = gym.spaces.Box(low=-1.0, high=0.0, shape=(256,), dtype = np.float16)

        self.reset()
        self.seed()

        self.global_steps = 0


    def seed(self, seed=None):
        self.np_random, seed = gym.utils.seeding.np_random(seed)
        return [seed]

    def reset(self, *, seed = None, options = None):

        flag = np.ones([nx, ny])

        action = flag.astype(np.float32)
        data_to_send = action.flatten().tolist()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_address = ('localhost', 8082)

        server_socket.connect(server_address)
        packed_data = msgpack.packb(data_to_send)

        while packed_data:
            sent = server_socket.send(packed_data)
            packed_data = packed_data[sent:]


        initial_state = np.zeros(obs_shape, dtype=np.float16)
        self.count = 0
        return initial_state, {}




    def step(self, action):

        epsilon_1 = 1
        epsilon_2 = 35
        C1 = 1
        C2 = 100
        BETA = 0.5
        factor = 8
        C3 = 1


        # Get the control variable (activity coefficient) from the action
        action = action.astype(np.float32)
        action = gaussian_filter( action.reshape(int(nx/factor), int(ny/factor)), sigma = 1 )
       # action = np.clip(action, -2, 0)
        action_interpolated = np.zeros([nx, ny])


        for i in range(int(nx/factor)):
            for j in range(int(ny/factor)):
                action_interpolated[i*factor:(i+1)*factor, j*factor:(j+1)*factor] = action[i, j]

        data_to_send = action_interpolated.flatten().tolist()

        self.global_steps += 1


        ###################### TIME STEPPING ##############################################
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_address = ('localhost', 8082)

        server_socket.connect(server_address)
        packed_data = msgpack.packb(data_to_send)

        while packed_data:
            sent = server_socket.send(packed_data)
            packed_data = packed_data[sent:]

        received_data_ = b""


        try:
            while len(received_data_) < x_bytes:
                chunk = server_socket.recv(x_bytes - len(received_data_))
                if not chunk:
                    print("Error: Connection closed by peer")
                    break
                received_data_ += chunk
        except ConnectionResetError:
            print("==> ConnectionResetError")
            k = np.random.random([4, N, N])
            k = k.flatten().tolist()
            received_data_ = msgpack.packb(k)
            pass

        #server_socket.close()

        unpacked_data = msgpack.unpackb(received_data_)
        chunk_size = len(unpacked_data) // 4
        states = [unpacked_data[i:i+chunk_size] for i in range(0, len(unpacked_data), chunk_size)]

        Qxx = gaussian_filter ( np.asarray(states[0]).reshape(nx, ny)[::2, ::2], sigma = 1 )
        Qxy = gaussian_filter ( np.asarray(states[1]).reshape(nx, ny)[::2, ::2], sigma = 1 )
        ux  = gaussian_filter ( np.asarray(states[2]).reshape(nx, ny)[::2, ::2], sigma = 1 )
        uy  = gaussian_filter ( np.asarray(states[3]).reshape(nx, ny)[::2, ::2], sigma = 1 )

        # print(f"step $$$$$$$$$$$$$$$$$$$$$$", self.global_steps)

        Qxx_ = (Qxx - np.min(Qxx))/(np.max(Qxx) - np.min(Qxx))
        Qxy_ = (Qxy - np.min(Qxy))/(np.max(Qxy) - np.min(Qxy))
        ux_ = (ux - np.min(ux))/(np.max(ux) - np.min(ux))
        uy_ = (uy - np.min(uy))/(np.max(uy) - np.min(uy))

        self.grid = np.stack([ux_, uy_, Qxx_, Qxy_], axis = 0)


        middle_start_col = ( int(obs_shape[1]/2) - int(H/2) ) 

        ux_mid = ux[middle_start_col:middle_start_col + int(H)+1, :]
        uy_mid = uy[middle_start_col:middle_start_col + int(H)+1, :]

        j_alpha = np.sum( (action_interpolated.reshape(nx, ny) + 0.01*np.ones([nx, ny]))**2 )
        reward_j_alpha = C1*np.power(0.5, 0.005*j_alpha)

        x = (np.mean(ux_mid) - 0.4)**2 + (np.mean(uy_mid) - 0)**2

        reward = C3*C1*np.power(BETA, C2*x) + C3*reward_j_alpha

        mean_ux = np.mean(ux_mid)

        ###################################################################
        
        if ( reward < epsilon_1 and self.count > self.time):         # too bad
            done = True

        elif ( reward > epsilon_2 and self.count > self.time): # good enough 
            done = True
        else:             
            self.count = self.count + 1     # continue learning 
            done = False


        info_dict = {
            "scalar_metrics": {"mean_ux": mean_ux}
        }
        return (
            self.grid,
            reward, 
            done,  
            False,
            info_dict
        )


if __name__ == "__main__":
    args = parser.parse_args()
    print(f"\nRunning with following CLI options: {args}\n")
    
  
    ray.init(
        local_mode=args.local_mode,
        _temp_dir=f"/scratch0/saptorshighosh/ray_sessions"
    )

    total_gpus = 2
    
    config = (
        get_trainable_cls(args.run)
        .get_default_config()
        .experimental(_disable_preprocessor_api=True)
        #.environment(FluidEnv, env_config={"time": 1000})
        .framework(args.framework)
        .api_stack(enable_rl_module_and_learner=True)
        .rl_module(
            rl_module_spec=SingleAgentRLModuleSpec(
                module_class=CustomTorchPPORLModule,
                model_config_dict={
                    "num_channels": [16,32,256], 
                    "shared": False, 
                    #"checkpoint": "/scratch0/saptorshighosh/ray_results/128_128/PPO_FluidEnv_33e94_00000_0_2024-07-31_22-46-07/checkpoint_000008/policies/default_policy/policy_state.pkl",
                    "checkpoint": None,
                    "ds": False
                    }, 
            )
        )
        #.rl_module(_enable_rl_module_api=True)
        #.training(_enable_learner_api=True)
        .rollouts(num_rollout_workers=1)
        .resources(
            num_learner_workers = 1,
            num_gpus_per_learner_worker = 0.5,
            num_cpus_per_worker = 8, 
            num_gpus_per_worker = 0.5
        )        
        # Use GPUs iff `RLLIB_NUM_GPUS` env var set to > 0.   
    )

    stop = {
        "training_iteration": args.stop_iters,
        "timesteps_total": args.stop_timesteps,
        "episode_reward_mean": args.stop_reward,
    }


    # Define the address and port for communication


    if args.no_tune:
        # manual training with train loop using PPO and fixed learning rate
        if args.run != "PPO":
            raise ValueError("Only support --run PPO with --no-tune.")
        print("Running manual train loop without Ray Tune.")
        # use fixed learning rate instead of grid search (needs tune)
        config.lr = 1e-3
        algo = config.build()
        # run manual training loop and print results after each iteration
        for i in range(args.stop_iters):
            result = algo.train()

            if (
                result["timesteps_total"] >= args.stop_timesteps
                or result["episode_reward_mean"] >= args.stop_reward
            ):
                break
        algo.stop()
    
    else:      
        param_space = config.to_dict()

        # automated run with Tune and grid search and TensorBoard
        vf_clip_param = 10

        param_space.update({
            "sgd_minibatch_size": 512,
            "train_batch_size": 2048,
            "vf_clip_param" : vf_clip_param,
            "lr": 1e-5
        })
        

        print("Training automatically with Ray Tune")
        tuner = tune.Tuner(
            args.run,
            param_space=param_space,
            run_config=air.RunConfig(
                stop=stop, 
                checkpoint_config=air.CheckpointConfig(checkpoint_frequency=30, checkpoint_at_end=True),
                verbose=3,
                storage_path = f"/scratch0/saptorshighosh/ray_results", 
                name = f"128_128"
            ),
        )
        results = tuner.fit()
    
        if args.as_test:
            print("Testing the agent...")
            print("Checking if learning goals were achieved")
            check_learning_achieved(results, args.stop_reward)
        
    ray.shutdown()

