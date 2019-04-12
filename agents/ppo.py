"""
Algorithm for Proximal Policy Optimization.

Note that this comes from my (Carroll's) self-training exercises.
It should probably be replaced with OpenAI baselines.
"""

import os
import logging
from types import SimpleNamespace
from collections import deque, namedtuple
from functools import wraps

import numpy as np
import tensorflow as tf

from .wrappers import VideoMonitor, AutoResetWrapper

logger = logging.getLogger(__name__)

DEFAULT_LOGDIR = os.path.join(__file__, '../../data/tmp')
DEFAULT_LOGDIR = os.path.abspath(DEFAULT_LOGDIR)


def named_output(*names):
    """
    A simple decorator to transform a function's output to a named tuple.
    """
    def decorator(func):
        rtype = namedtuple(func.__name__ + '_rval', names)

        @wraps(func)
        def wrapped(*args, **kwargs):
            rval = func(*args, **kwargs)
            if isinstance(rval, tuple):
                rval = rtype(*rval)
            return rval
        return wrapped

    return decorator


def discounted_rewards(rewards, gamma, final=0.0):
    d = [final]
    for r in rewards[::-1]:
        d.append(r + d[-1] * gamma)
    return np.array(d[:0:-1])


def shuffle_arrays_in_place(*data):
    """
    This runs np.random.shuffle on multiple inputs, shuffling each in place
    in the same order (assuming they're the same length).
    """
    rng_state = np.random.get_state()
    for x in data:
        np.random.set_state(rng_state)
        np.random.shuffle(x)


def shuffle_arrays(*data):
    # Despite the nested for loops, this is actually a little bit faster
    # than the above because it doesn't involve any copying of array elements.
    # When the array elements are large (like environment states),
    # that overhead can be large.
    idx = np.random.permutation(len(data[0]))
    return [[x[i] for i in idx] for x in data]


def eps_relu(x, eps):
    return tf.maximum(x, -eps)


def eps_elu(x, eps):
    return eps * tf.nn.elu(x / eps)


class PPO(object):
    """
    Proximal policy optimization.
    """
    gamma = 0.99
    lmda = 0.95  # generalized advantage estimation parameter
    learning_rate = 1e-4
    entropy_reg = 0.01
    entropy_clip = 1.0  # don't start regularization until it drops below this
    vf_coef = 0.5
    max_gradient_norm = 5.0
    eps_clip = 0.2  # PPO clipping for both value and policy losses
    reward_clip = 0.0
    value_grad_rescaling = 'smooth'  # one of [False, 'smooth', 'per_batch', 'per_state']
    policy_rectifier = 'relu'  # or 'elu' or ...more to come
    scale_prob_clipping = False

    video_freq = 20

    _params_loaded = False

    def __init__(self, envs, logdir=DEFAULT_LOGDIR, saver_args={}, **kwargs):
        for key, val in kwargs.items():
            if (not key.startswith('_') and hasattr(self, key) and
                    not callable(getattr(self, key))):
                setattr(self, key, val)
            else:
                raise ValueError("Unrecognized parameter: '%s'" % (key,))

        if callable(envs):
            envs = [envs() for _ in range(self.num_env)]
        else:
            envs = list(envs)
        envs[0] = VideoMonitor(envs[0], logdir, self.next_video_name)
        self.envs = [AutoResetWrapper(env) for env in envs]

        self.op = SimpleNamespace()
        self.num_steps = 0
        self.num_episodes = 0
        self.build_graph()
        self.session = tf.Session()
        self.session.run(tf.global_variables_initializer())
        self.logger = tf.summary.FileWriter(logdir, self.session.graph)
        self.saver = tf.train.Saver(**saver_args)
        self.save_path = os.path.join(logdir, 'model')
        self.recent_states = deque(maxlen=1)
        self.training_stats = deque(maxlen=1)
        self.restore_checkpoint(logdir)

    def save_checkpoint(self):
        logger.info("Saving new checkpoint. %i episodes, %i steps.",
                    self.num_episodes, self.num_steps)
        self.op.num_steps.load(self.num_steps, self.session)
        self.op.num_episodes.load(self.num_episodes, self.session)
        self.saver.save(self.session, self.save_path, self.num_steps)

    def restore_checkpoint(self, logdir):
        """
        Resume training from the specified directory.

        If the directory is empty, don't load.
        """
        # Annoyingly, tf.train.latest_checkpoint fails if the directory
        # has changed. Instead, load up from the current directory so that
        # we're able to rerun training locally that was started remotely.
        import re
        checkpoint_path = os.path.join(logdir, 'checkpoint')
        if not os.path.exists(checkpoint_path):
            return
        with open(checkpoint_path) as checkpoint_file:
            line = checkpoint_file.readline()
        match = re.match(r'.*"(.+)"', line)
        if not match:
            return
        last_checkpoint = os.path.split(match.group(1))[1]
        last_checkpoint = os.path.join(logdir, last_checkpoint)
        try:
            self.saver.restore(self.session, last_checkpoint)
        except ValueError:
            return
        self.num_steps, self.num_episodes = self.session.run(
            [self.op.num_steps, self.op.num_episodes])
        logger.info("Restoring old checkpoint. %i episodes, %i steps.",
                    self.num_episodes, self.num_steps)

    def build_graph(self):
        op = self.op
        input_space = self.envs[0].observation_space
        op.states = tf.placeholder(
            input_space.dtype, [None] + list(input_space.shape), name="state")
        op.actions = tf.placeholder(tf.int32, [None], name="actions")
        op.rewards = tf.placeholder(tf.float32, [None], name="rewards")
        op.advantages = tf.placeholder(tf.float32, [None], name="advantages")
        op.old_policy = tf.placeholder(tf.float32, [None], name="old_policy")
        op.old_value = tf.placeholder(tf.float32, [None], name="old_value")
        op.learning_rate = tf.constant(self.learning_rate, name="learning_rate")
        op.eps_clip = tf.constant(self.eps_clip, name="eps_clip")
        op.num_steps = tf.get_variable('num_steps', initializer=tf.constant(0))
        op.num_episodes = tf.get_variable('num_episodes', initializer=tf.constant(0))

        with tf.name_scope("policy"):
            op.logits, op.v = self.build_logits_and_values(op.states)
            op.policy = tf.nn.softmax(op.logits)
            num_actions = op.policy.shape[-1].value
        with tf.name_scope("policy_loss"):
            hot_actions = tf.one_hot(op.actions, num_actions, dtype=tf.float32)
            a_policy = tf.reduce_mean(op.policy * hot_actions, axis=-1)
            prob_diff = tf.sign(op.advantages) * (1 - a_policy / op.old_policy)
            if self.scale_prob_clipping:
                # Scaling the clipping by 1 - old_policy ensures that
                # the clipping is active even when the new policy is 1.
                # This is non-standard.
                eps = op.eps_clip * (1 - op.old_policy)
            else:
                eps = op.eps_clip
            rectifier = {
                'relu': eps_relu,
                'elu': eps_elu,
            }[self.policy_rectifier]
            policy_loss = op.advantages * rectifier(prob_diff, eps)
            policy_loss = tf.reduce_mean(policy_loss)
        with tf.name_scope("entropy"):
            op.entropy = tf.reduce_sum(
                -op.policy * tf.log(op.policy + 1e-12), axis=-1)
            mean_entropy = tf.reduce_mean(op.entropy)
            pseudo_entropy = tf.stop_gradient(
                tf.reduce_sum(op.policy*(1-op.policy), axis=-1))
            avg_pseudo_entropy = tf.reduce_mean(pseudo_entropy)
            smoothed_pseudo_entropy = tf.get_variable(
                'smoothed_pseudo_entropy', initializer=tf.constant(1.0))
            # The first term in the entropy loss encourages higher entropy
            # in the policy, encouraging exploration.
            # Note that this uses the pseudo-entropy rather than the
            # conventional entropy. This is because the derivative of the
            # normal entropy diverges at zero.
            entropy_loss = -self.entropy_reg * tf.minimum(avg_pseudo_entropy, self.entropy_clip)
            # The second term in the entropy loss is just used to adjust the
            # smoothed pseudo entropy.
            entropy_loss += 0.5 * tf.square(avg_pseudo_entropy - smoothed_pseudo_entropy)
        with tf.name_scope("value_loss"):
            v_clip = op.old_value + tf.clip_by_value(
                op.v - op.old_value, -op.eps_clip, op.eps_clip)
            value_loss = tf.maximum(
                tf.square(op.v - op.rewards), tf.square(v_clip - op.rewards))
            # Rescale the value function with entropy.
            # The gradient of the policy function becomes very small when
            # the entropy is very low, essentially because it means the softmax
            # of the policy logits is being saturated. By rescaling the value
            # loss we attempt to make it have the same relative importance
            # as the policy loss. Not clear how necessary this is.
            if self.value_grad_rescaling == 'per_state':
                value_loss *= pseudo_entropy
            elif self.value_grad_rescaling == 'per_batch':
                value_loss *= avg_pseudo_entropy
            elif self.value_grad_rescaling == 'smooth':
                value_loss *= tf.stop_gradient(smoothed_pseudo_entropy)
            elif self.value_grad_rescaling:
                raise ValueError("Unrecognized value reweighting type: '%s'" % (
                    self.value_grad_rescaling,))
            value_loss = 0.5 * tf.reduce_mean(value_loss)

        with tf.name_scope("trainer"):
            total_loss = policy_loss + value_loss * self.vf_coef + entropy_loss
            optimizer = self.build_optimizer(op.learning_rate)
            grads, variables = zip(*optimizer.compute_gradients(total_loss))
            op.grads = grads
            if self.max_gradient_norm > 0:
                grads2, _ = tf.clip_by_global_norm(grads, self.max_gradient_norm)
            op.train = optimizer.apply_gradients(zip(grads2, variables))

        with tf.name_scope("network"):
            tf.summary.scalar("value_func", tf.reduce_mean(op.v))
            tf.summary.histogram("value_func", op.v)
            tf.summary.scalar("entropy", mean_entropy)
            tf.summary.histogram("entropy", op.entropy)
            tf.summary.scalar("pseudo_entropy", avg_pseudo_entropy)
            tf.summary.scalar("pseudo_entropy_smooth", smoothed_pseudo_entropy)
        with tf.name_scope("training"):
            op.training_stats = tf.stack([
                tf.global_norm(grads),
                policy_loss,
                value_loss
            ])
            op.training_stats_batch = tf.placeholder(tf.float32, [None, 3])
            tf.summary.histogram("gradients", op.training_stats_batch[:,0])
            tf.summary.histogram("policy_loss", op.training_stats_batch[:,1])
            tf.summary.histogram("value_loss", op.training_stats_batch[:,2])
        op.summary = tf.summary.merge_all()

    def build_optimizer(self, learning_rate):
        return tf.train.AdamOptimizer(
            learning_rate=learning_rate,
            epsilon=1e-6,
        )

    def build_logits_and_values(self, states):
        raise NotImplementedError

    @named_output('s', 'a', 'pi', 'r', 'A', 'v')
    def gen_batch(self, steps_per_env, as_np_arrays=False, old_reward=False):
        op = self.op
        session = self.session
        num_env = len(self.envs)

        states = []
        actions = []
        old_policy = []
        rewards = []
        values = []
        mask = []

        # Run agents through the environment
        for _ in range(steps_per_env):
            states += [env.state for env in self.envs]
            policies, vals = session.run([op.policy, op.v], feed_dict={
                op.states: states[-num_env:]
            })
            values.append(vals)
            for env, policy in zip(self.envs, policies):
                self.num_steps += 1
                action = np.random.choice(len(policy), p=policy)
                _, reward, done, info = env.step(action)
                rewards.append(reward)
                mask.append(not done)
                actions.append(action)
                old_policy.append(policy[action])
                if done:
                    self.log_episode(info)
        self.recent_states += states

        # Get the value of the last state (for discounted rewards)
        values.append(session.run(op.v, feed_dict={
            op.states: [env.state for env in self.envs]
        }))

        # Make the discounted rewards and advantages
        values = np.array(values)
        rewards = np.array(rewards).reshape(-1, num_env)
        if self.reward_clip > 0:
            rewards = np.clip(rewards, -self.reward_clip, self.reward_clip)
        if old_reward:
            old_rewards = rewards.ravel().copy()
        mask = np.array(mask).reshape(-1, num_env)
        advantages = rewards + self.gamma * mask * values[1:] - values[:-1]
        rewards[-1] += mask[-1] * self.gamma * values[-1]
        for i in range(len(rewards)-2, -1, -1):
            rewards[i] += self.gamma * mask[i] * rewards[i+1]
            advantages[i] += (self.gamma * self.lmda) * mask[i] * advantages[i+1]
        rewards = rewards.ravel()
        advantages = advantages.ravel()
        values = values[:-1].ravel()

        if as_np_arrays:
            states = np.array(states)
            actions = np.array(actions)
            old_policy = np.array(old_policy)
        if old_reward:
            # Include undiscounted rewards too (for debugging)
            rewards = [old_rewards, rewards]

        return states, actions, old_policy, rewards, advantages, values

    def next_video_name(self):
        if self.num_episodes % self.video_freq == 0:
            return "video_{}-{:0.3g}".format(self.num_episodes, self.num_steps)

    def train_batch(self, steps_per_env=20, batch_size=32, epochs=3):
        op = self.op
        session = self.session
        num_env = len(self.envs)

        batch = self.gen_batch(steps_per_env)

        # Do the training
        states, actions, old_policy, rewards, advantages, values = shuffle_arrays(*batch)
        for _ in range(epochs):
            for k in range(0, len(states) + 1 - batch_size, batch_size):
                k2 = k+batch_size
                stats, _ = session.run([op.training_stats, op.train], feed_dict={
                    op.states: states[k:k2],
                    op.actions: actions[k:k2],
                    op.old_policy: old_policy[k:k2],
                    op.old_value: values[k:k2],
                    op.rewards: rewards[k:k2],
                    op.advantages: advantages[k:k2],
                })
                self.training_stats.append(stats)

        return steps_per_env * num_env

    def log_episode(self, info):
        self.num_episodes += 1
        summary = tf.Summary()
        summary.value.add(tag='episode/reward', simple_value=info['episode_reward'])
        summary.value.add(tag='episode/length', simple_value=info['episode_length'])
        summary.value.add(tag='episode/completed', simple_value=self.num_episodes)
        self.logger.add_summary(summary, self.num_steps)
        logger.info(
            "Episode %i: length=%i, reward=%0.1f",
            self.num_episodes, info['episode_length'], info['episode_reward'])

    def train(self, total_steps, report_every=2000, save_every=5000, **kw):
        last_save = self.num_steps - 1
        self.recent_states = deque(maxlen=report_every)
        self.training_stats = deque(maxlen=report_every)
        while self.num_steps < total_steps:
            self.train_batch(**kw)
            if last_save // save_every < self.num_steps // save_every:
                self.save_checkpoint()
                last_save = self.num_steps
            if len(self.recent_states) >= report_every:
                summary = self.session.run(self.op.summary, feed_dict={
                    self.op.states: list(self.recent_states),
                    self.op.training_stats_batch: np.array(self.training_stats),
                })
                self.logger.add_summary(summary, self.num_steps)
                self.recent_states.clear()
                self.training_stats.clear()
        logger.info("FINISHED TRAINING")
