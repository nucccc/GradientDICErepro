#######################################################################
# Copyright (C) 2017 Shangtong Zhang(zhangshangtong.cpp@gmail.com)    #
# Permission given to modify the code as long as you keep this        #
# declaration at the top                                              #
#######################################################################

from ..network import *
from ..component import *
from .BaseAgent import *
import torchvision


class TD3CorrectionAgent(BaseAgent):
    def __init__(self, config):
        BaseAgent.__init__(self, config)
        self.config = config
        self.task = config.task_fn()
        self.network = config.network_fn()
        self.target_network = config.network_fn()
        self.target_network.load_state_dict(self.network.state_dict())
        self.replay = config.replay_fn()
        self.random_process = config.random_process_fn()
        self.total_steps = 0
        self.state = None

        self.DICENet = config.dice_net_fn()

    def soft_update(self, target, src):
        for target_param, param in zip(target.parameters(), src.parameters()):
            target_param.detach_()
            target_param.copy_(target_param * (1.0 - self.config.target_network_mix) +
                               param * self.config.target_network_mix)

    def eval_step(self, state):
        self.config.state_normalizer.set_read_only()
        state = self.config.state_normalizer(state)
        action = self.network(state)
        self.config.state_normalizer.unset_read_only()
        return to_np(action)

    def compute_correction(self, states, actions):
        config = self.config
        if config.correction == 'no':
            correction = 1
        elif config.correction in ['GradientDICE', 'GenDICE']:
            correction = self.DICENet.tau(states, actions).detach()
        else:
            raise NotImplementedError
        return correction

    def step(self):
        config = self.config
        if self.state is None:
            self.random_process.reset_states()
            self.state = self.task.reset()
            self.state = config.state_normalizer(self.state)

        action = [self.task.action_space.sample()]
        # if self.total_steps < config.warm_up:
        #     action = [self.task.action_space.sample()]
        # else:
        #     action = self.network(self.state)
        #     action = to_np(action)
        #     action += self.random_process.sample()
        action = np.clip(action, self.task.action_space.low, self.task.action_space.high)
        next_state, reward, done, info = self.task.step(action)
        next_state = self.config.state_normalizer(next_state)
        self.record_online_return(info)
        reward = self.config.reward_normalizer(reward)

        experiences = list(zip(self.state, action, reward, next_state, done))
        self.replay.feed_batch(experiences)
        if done[0]:
            self.random_process.reset_states()
        self.state = next_state
        self.total_steps += 1

        if self.replay.size() >= config.warm_up:
            experiences = self.replay.sample()
            states, actions, rewards, next_states, terminals = experiences
            states = tensor(states)
            actions = tensor(actions)
            rewards = tensor(rewards).unsqueeze(-1)
            next_states = tensor(next_states)
            mask = tensor(1 - terminals).unsqueeze(-1)
            self.train_dice(states, actions, next_states, mask)

            a_next = self.target_network(next_states)
            noise = torch.randn_like(a_next).mul(config.td3_noise)
            noise = noise.clamp(-config.td3_noise_clip, config.td3_noise_clip)

            min_a = float(self.task.action_space.low[0])
            max_a = float(self.task.action_space.high[0])
            a_next = (a_next + noise).clamp(min_a, max_a)

            q_1, q_2 = self.target_network.q(next_states, a_next)
            target = rewards + config.discount * mask * torch.min(q_1, q_2)
            target = target.detach()

            q_1, q_2 = self.network.q(states, actions)
            # cor = self.compute_correction(states, actions)
            cor = 1
            critic_loss = F.mse_loss(cor * q_1, cor * target) +\
                          F.mse_loss(cor * q_2, cor * target)

            self.network.zero_grad()
            critic_loss.backward()
            self.network.critic_opt.step()

            if self.total_steps % config.td3_delay:
                action = self.network(states)
                cor = self.compute_correction(states, action)
                self.logger.add_histogram('ratio', cor, log_level=5)
                policy_loss = -self.network.q(states, action)[0].mul(cor).mean()

                self.network.zero_grad()
                policy_loss.backward()
                self.network.actor_opt.step()

                self.soft_update(self.target_network, self.network)

    def train_dice(self, states, actions, next_states, masks):
        config = self.config
        if config.correction == 'no':
            return

        next_actions = self.network(next_states).detach()
        states_0 = tensor(config.sample_init_states())
        actions_0 = self.network(states_0).detach()

        tau = self.DICENet.tau(states, actions)
        f = self.DICENet.f(states, actions)
        f_next = self.DICENet.f(next_states, next_actions)
        f_0 = self.DICENet.f(states_0, actions_0)
        u = self.DICENet.u(states.size(0))

        if config.correction == 'GenDICE':
            J_concave = (1 - config.discount) * f_0 + config.discount * tau.detach() * f_next - \
                        tau.detach() * (f + 0.25 * f.pow(2)) + config.lam * (u * tau.detach() - u - 0.5 * u.pow(2))
            J_convex = (1 - config.discount) * f_0.detach() + config.discount * tau * f_next.detach() - \
                       tau * (f.detach() + 0.25 * f.detach().pow(2)) + \
                       config.lam * (u.detach() * tau - u.detach() - 0.5 * u.detach().pow(2))
        elif config.correction == 'GradientDICE':
            J_concave = (1 - config.discount) * f_0 + config.discount * tau.detach() * f_next - \
                        tau.detach() * f - 0.5 * f.pow(2) + config.lam * (u * tau.detach() - u - 0.5 * u.pow(2))
            J_convex = (1 - config.discount) * f_0.detach() + config.discount * tau * f_next.detach() - \
                       tau * f.detach() - 0.5 * f.detach().pow(2) + \
                       config.lam * (u.detach() * tau - u.detach() - 0.5 * u.detach().pow(2))
        else:
            raise NotImplementedError

        loss = (J_convex - J_concave).mul(masks).mean()
        # loss = J_convex.mean() - J_concave.mean()
        self.DICENet.opt.zero_grad()
        loss.backward()
        self.DICENet.opt.step()