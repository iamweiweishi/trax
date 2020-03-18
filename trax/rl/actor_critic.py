# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Classes for RL training in Trax."""

import functools
import os
import jax
import numpy as np

from trax import layers as tl
from trax import lr_schedules as lr
from trax import shapes
from trax import supervised
from trax.rl import distributions
from trax.rl import training as rl_training


class ActorCriticTrainer(rl_training.PolicyTrainer):
  """Trains policy and value models using actor-critic methods."""

  def __init__(self, task,
               value_model=None,
               value_optimizer=None,
               value_lr_schedule=lr.MultifactorSchedule,
               value_batch_size=64,
               value_train_steps_per_epoch=500,
               n_shared_layers=0,
               on_policy=True,
               **kwargs):  # Arguments of PolicyTrainer come here.
    """Configures the actor-critic Trainer."""
    self._on_policy = on_policy
    self._n_shared_layers = n_shared_layers
    self._value_batch_size = value_batch_size
    self._value_train_steps_per_epoch = value_train_steps_per_epoch

    # The 2 below will be initalized in super.__init__ anyway, but are needed
    # to construct value batches which are needed before PolicyTrainer init
    # since policy input creation calls the value model -- hence this code.
    self._task = task
    self._max_slice_length = kwargs.get('max_slice_length', None)

    # Initialize training of the value function.
    value_output_dir = kwargs.get('output_dir', None)
    if value_output_dir is not None:
      value_output_dir = os.path.join(value_output_dir, '/value')
    self._value_inputs = supervised.Inputs(
        train_stream=lambda _: self.value_batches_stream())
    self._value_trainer = supervised.Trainer(
        model=value_model,
        optimizer=value_optimizer,
        lr_schedule=value_lr_schedule,
        loss_fn=tl.L2Loss,
        inputs=self._value_inputs,
        output_dir=value_output_dir,
        metrics={'value_loss': tl.L2Loss},
        has_weights=True)
    self._value_eval_model = value_model(mode='eval')
    value_batch = next(self.value_batches_stream())
    self._value_eval_model.init(value_batch)

    # Initialize policy training.
    super(ActorCriticTrainer, self).__init__(task, **kwargs)

  def value_batches_stream(self):
    """Use the RLTask self._task to create inputs to the value model."""
    for np_trajectory in self._task.trajectory_batch_stream(
        self._value_batch_size, max_slice_length=self._max_slice_length):
      # Insert an extra depth dimension, so the target shape is consistent with
      # the network output shape.
      yield (np_trajectory.observations,         # Inputs to the value model.
             np_trajectory.returns[:, :, None],  # Targets: regress to returns.
             np_trajectory.mask[:, :, None])     # Mask to zero-out padding.

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values.

    Args:
      trajectory: a TrajectoryNp, the trajectory to create inputs from
      values: a numpy array: value function computed on trajectory

    Returns:
      a tuple of numpy arrays of the form (inputs, x1, x2, ...) that will be
      passed to the policy model; policy model will compute outputs from
      inputs and (outputs, x1, x2, ...) will be passed to self.policy_loss
      which should be overridden accordingly.
    """
    return NotImplementedError

  def policy_batches_stream(self):
    """Use the RLTask self._task to create inputs to the policy model."""
    if self._on_policy:
      epochs = [-1]
    else:
      epochs = None
    for np_trajectory in self._task.trajectory_batch_stream(
        self._policy_batch_size,
        epochs=epochs,
        max_slice_length=self._max_slice_length + 1,
        include_final_state=True,
        sample_trajectories_uniformly=True):
      value_model = self._value_eval_model
      value_model.weights = self._value_trainer.model_weights
      values = value_model(np_trajectory.observations, n_accelerators=1)
      shapes.assert_shape_equals(
          values, (self._policy_batch_size, self._max_slice_length + 1, 1)
      )
      values = np.squeeze(values, axis=2)  # Remove the singleton depth dim.
      yield self.policy_inputs(np_trajectory, values)

  def train_epoch(self):
    """Trains RL for one epoch."""
    self._value_trainer.train_epoch(self._value_train_steps_per_epoch, 1)
    if self._n_shared_layers > 0:  # Copy value weights to policy trainer.
      value_weights = self._value_trainer.model_weights
      policy_weights = self._policy_trainer.model_weights
      shared_weights = value_weights[:self._n_shared_layers]
      policy_weights[:self._n_shared_layers] = shared_weights
      self._policy_trainer.model_weights = policy_weights
    self._policy_trainer.train_epoch(self._policy_train_steps_per_epoch, 1)
    if self._n_shared_layers > 0:  # Copy policy weights to value trainer.
      value_weights = self._value_trainer.model_weights
      policy_weights = self._policy_trainer.model_weights
      shared_weights = policy_weights[:self._n_shared_layers]
      value_weights[:self._n_shared_layers] = shared_weights
      self._value_trainer.model_weights = value_weights


class AWRTrainer(ActorCriticTrainer):
  """Trains policy and value models using AWR."""

  def __init__(self, task, beta=1.0, w_max=20.0, **kwargs):
    """Configures the AWR Trainer."""
    self._beta = beta
    self._w_max = w_max
    super(AWRTrainer, self).__init__(task,
                                     on_policy=False,
                                     **kwargs)

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values."""
    rew = trajectory.rewards[:, :-1]
    td_advantage = rew + self._task.gamma * values[:, 1:] - values[:, :-1]
    # advantage = trajectory.returns[:, :-1] - values[:, :-1]
    awr_weights = np.minimum(np.exp(td_advantage / self._beta), self._w_max)
    # Observations should be the same length as awr_weights - so if we are
    # using td_advantage, we need to cut one (as we need the value of
    # one more state to subtract.
    return (
        trajectory.observations[:, :-1],
        trajectory.actions[:, :-1],
        awr_weights)

  @property
  def policy_loss(self):
    """Policy loss."""
    return functools.partial(
        distributions.LogLoss, distribution=self._policy_dist)


class AdvantageActorCriticTrainer(ActorCriticTrainer):
  """The Advantage Actor Critic Algorithm aka A2C.

  Trains policy and value models using the A2C algortithm.
  """

  def __init__(self, task, **kwargs):
    """Configures the a2c Trainer."""
    super(AdvantageActorCriticTrainer, self).__init__(task, **kwargs)

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values."""
    advantages = trajectory.returns[:, :-1] - values[:, :-1]
    return (
        trajectory.observations,
        trajectory.actions,
        advantages)

  @property
  def policy_loss(self):
    """Policy loss."""
    return functools.partial(
        distributions.LogLoss, distribution=self._policy_dist)


class PPOTrainer(ActorCriticTrainer):
  """The Proximal Policy Optimization Algorithm aka PPO.

  Trains policy and value models using the PPO algortithm.
  """

  def __init__(self, task, ppo_eps=0.2, **kwargs):
    """Configures the a2c Trainer."""
    self.ppo_eps = ppo_eps
    super(PPOTrainer, self).__init__(task, **kwargs)

  def policy_inputs(self, trajectory, values):
    """Create inputs to policy model from a TrajectoryNp and values."""
    advantages = trajectory.returns[:, :-1] - values[:, :-1]
    # The old log_probs were recorder together with the trajectory
    old_log_probs = trajectory.log_probs[:, :-1]

    return (
        trajectory.observations,
        trajectory.actions,
        advantages,
        old_log_probs)

  # How do we make sure that log_probs from the network end up as
  # new_log_probs?
  def ppo_loss(self, new_log_probs, advantages, old_log_probs, **kwargs):
    # here we are working around the fact that we record
    # log_probs in the trajectory (and not probs)
    probs_ratio = np.exp(new_log_probs)/np.exp(self.old_log_probs)
    unclipped_objective = probs_ratio * advantages
    clipped_objective = jax.lax.clamp(1 - self.ppo_eps,
                                      probs_ratio,
                                      1 + self.ppo_eps) * advantages
    # calculate the min of the two objectives
    stacked_objectives = np.stack([unclipped_objective, clipped_objective], 0)
    ppo_objective = np.min(stacked_objectives, 0)
    # the nial target should be the minus average of the objective
    return -ppo_objective.mean()

  @property
  def policy_loss(self):
    """Policy loss."""
    return self.ppo_loss

