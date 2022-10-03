""" Builds a network to read time from clocks.

Summary of available functions:

 # Compute inference on the model inputs to make a prediction.
 predictions = inference(inputs)

 # Compute the total loss of the prediction with respect to the labels.
 loss = loss(predictions, labels)

 # Create a graph to run one step of training with respect to the loss.
 train_op = train(loss, global_step)


This model is essentially the same as the cifar10 model:
https://github.com/tensorflow/tensorflow/tree/master/tensorflow/models/image/cifar10

"""
# pylint: disable=missing-docstring
from __future__ import division
from __future__ import print_function

import re
import tensorflow as tf
import numpy as np

import clock_data

FLAGS = tf.compat.v1.app.flags.FLAGS

# Basic model parameters.
tf.compat.v1.app.flags.DEFINE_integer('batch_size', 128,
                            """Number of images to process in a batch.""")

# Global constants describing the clock data set.
IMAGE_SIZE1 = clock_data.image_size1
IMAGE_SIZE2 = clock_data.image_size2
NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 50
NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 10

# Constants describing the training process.
MOVING_AVERAGE_DECAY = 0.9999  # The decay to use for the moving average.
NUM_EPOCHS_PER_DECAY = 700.0  # Epochs after which learning rate decays.
LEARNING_RATE_DECAY_FACTOR = 0.1  # Learning rate decay factor.
INITIAL_LEARNING_RATE = 0.1  # Initial learning rate.

# If a model is trained with multiple GPUs, prefix all Op names with tower_name
# to differentiate the operations. Note that this prefix is removed from the
# names of the summaries when visualizing a model.
TOWER_NAME = 'tower'

tf.compat.v1.disable_eager_execution()

def _activation_summary(x):
    """Helper to create summaries for activations.

    Creates a summary that provides a histogram of activations.
    Creates a summary that measure the sparsity of activations.

    Args:
      x: Tensor
    Returns:
      nothing
    """
    # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
    # session. This helps the clarity of presentation on tensorboard.
    tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
    tf.summary.histogram(tensor_name + '/activations', x)
    tf.summary.scalar(tensor_name + '/sparsity', tf.nn.zero_fraction(x))


def _variable_on_cpu(name, shape, initializer):
    """Helper to create a Variable stored on CPU memory.

    Args:
      name: name of the variable
      shape: list of ints
      initializer: initializer for Variable

    Returns:
      Variable Tensor
    """
    with tf.device('/cpu:0'):
        var = tf.compat.v1.get_variable(name, shape, initializer=initializer,
                              dtype=tf.float32)
    return var


def _variable_with_weight_decay(name, shape, stddev, wd):
    """Helper to create an initialized Variable with weight decay.

    Note that the Variable is initialized with a truncated normal distribution.
    A weight decay is added only if one is specified.

    Args:
      name: name of the variable
      shape: list of ints
      stddev: standard deviation of a truncated Gaussian
      wd: add L2Loss weight decay multiplied by this float. If None, weight
          decay is not added for this Variable.

    Returns:
      Variable Tensor
    """
    var = _variable_on_cpu(
        name,
        shape,
        tf.compat.v1.truncated_normal_initializer(stddev=stddev, dtype=tf.float32))
    if wd is not None:
        weight_decay = tf.multiply(tf.nn.l2_loss(var), wd, name='weight_loss')
        tf.compat.v1.add_to_collection('losses', weight_decay)
    return var


def inference(images, num_classes):
    """ Build a time reading model for *either* hours or minutes.

    Args:
      images: Images returned from distorted_inputs() or inputs().
      num_classes: 12 for hours, 60 for minutes.

    Returns:
      Logits.
    """

    local4 = _inference_shared(images)

    dim = num_classes

    # softmax, i.e. softmax(WX + b)
    with tf.compat.v1.variable_scope('softmax_linear') as scope:
        weights = _variable_with_weight_decay('weights', [192, dim],
                                              stddev=1 / 192.0, wd=0.0)
        biases = _variable_on_cpu('biases', [dim],
                                  tf.constant_initializer(0.0))
        softmax_linear = tf.add(tf.matmul(local4, weights), biases,
                                name=scope.name)
        _activation_summary(softmax_linear)
    return softmax_linear


def inference_multitask(images):
    """
    Builds a time reading model that predicts hours *and* minutes in a
    multi-task setting.

    The model shares its structure and parameters until the final layer, which
    is separated into a classifier for hours and one for minutes.

    This is almost the same model as the single-class one, just with two
    outputs instead of one.

    :param images: Input to to the model.
    :return: tuple of softmax: hours and minutes.
    """
    local4 = _inference_shared(images)

    # softmax, i.e. softmax(WX + b)
    with tf.compat.v1.variable_scope('softmax_linear_hours') as scope:
        dim = 12
        weights = _variable_with_weight_decay('weights', [192, dim],
                                              stddev=1 / 192.0, wd=0.0)
        biases = _variable_on_cpu('biases', [dim],
                                  tf.constant_initializer(0.0))
        softmax_linear_hours = tf.add(tf.matmul(local4, weights), biases,
                                      name=scope.name)
        _activation_summary(softmax_linear_hours)

    with tf.compat.v1.variable_scope('softmax_linear_minutes') as scope:
        dim = 60
        weights = _variable_with_weight_decay('weights', [192, dim],
                                              stddev=1 / 192.0, wd=0.0)
        biases = _variable_on_cpu('biases', [dim],
                                  tf.constant_initializer(0.0))
        softmax_linear_minutes = tf.add(tf.matmul(local4, weights), biases,
                                        name=scope.name)
        _activation_summary(softmax_linear_minutes)

    return softmax_linear_hours, softmax_linear_minutes


def _inference_shared(images):
    """
    Build the shared layers of the inference model, which can then be used for
    *either* the single-task or multi-task learning objective.

    :param images:
    :return:
    """

    # We instantiate all variables using tf.get_variable() instead of
    # tf.Variable() in order to share variables across multiple GPU training
    # runs. If we only ran this model on a single GPU, we could simplify this
    # function by replacing all instances of tf.get_variable() with
    # tf.Variable().

    # conv1
    with tf.compat.v1.variable_scope('conv1') as scope:
        kernel = _variable_with_weight_decay('weights',
                                             shape=[5, 5, 1, 64],
                                             stddev=5e-2,
                                             wd=0.0)
        conv = tf.nn.conv2d(images, kernel, [1, 1, 1, 1], padding='SAME')
        biases = _variable_on_cpu('biases', [64], tf.constant_initializer(0.0))
        bias = tf.nn.bias_add(conv, biases)
        conv1 = tf.nn.relu(bias, name=scope.name)
        _activation_summary(conv1)

    # pool1
    pool1 = tf.nn.max_pool(conv1, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1],
                           padding='SAME', name='pool1')
    # norm1
    norm1 = tf.nn.lrn(pool1, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
                      name='norm1')

    # conv2
    with tf.compat.v1.variable_scope('conv2') as scope:
        kernel = _variable_with_weight_decay('weights',
                                             shape=[5, 5, 64, 64],
                                             stddev=5e-2,
                                             wd=0.0)
        conv = tf.nn.conv2d(norm1, kernel, [1, 1, 1, 1], padding='SAME')
        biases = _variable_on_cpu('biases', [64], tf.constant_initializer(0.1))
        bias = tf.nn.bias_add(conv, biases)
        conv2 = tf.nn.relu(bias, name=scope.name)
        _activation_summary(conv2)

    # norm2
    norm2 = tf.nn.local_response_normalization(
        conv2, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
        name='norm2')
    # pool2
    pool2 = tf.nn.max_pool(norm2, ksize=[1, 3, 3, 1],
                           strides=[1, 2, 2, 1], padding='SAME', name='pool2')

    # local3
    with tf.compat.v1.variable_scope('local3') as scope:
        # Move everything into depth so we can perform a single matrix multiply.
        batch_size = int(images.get_shape()[0])
        reshape = tf.reshape(pool2, [batch_size, -1])
        dim = reshape.get_shape()[1]
        weights = _variable_with_weight_decay('weights', shape=[dim, 384],
                                              stddev=0.04, wd=0.004)
        biases = _variable_on_cpu('biases', [384], tf.constant_initializer(0.1))
        local3 = tf.nn.relu(tf.matmul(reshape, weights) + biases,
                            name=scope.name)
        _activation_summary(local3)

    # local4
    with tf.compat.v1.variable_scope('local4') as scope:
        weights = _variable_with_weight_decay('weights', shape=[384, 192],
                                              stddev=0.04, wd=0.004)
        biases = _variable_on_cpu('biases', [192], tf.constant_initializer(0.1))
        local4 = tf.nn.relu(tf.matmul(local3, weights) + biases,
                            name=scope.name)
        _activation_summary(local4)
    return local4


def loss(logits, labels):
    return _loss_shared(logits, labels)


def loss_multitask(logits_h, labels_h,
                   logits_m, labels_m):
    loss_hours = _loss_shared(logits_h, labels_h)
    loss_minutes = _loss_shared(logits_m, labels_m)

    # Set the name of the variable in this way.
    return tf.add_n([loss_hours, loss_minutes], name='loss')


def _loss_shared(logits, labels):
    """Add L2Loss to all the trainable variables.

    Add summary for "Loss" and "Loss/avg".
    Args:
      logits: Logits from inference().
      labels: Labels from distorted_inputs or inputs(). 1-D tensor
              of shape [batch_size]

    Returns:
      Loss tensor of type float.
    """
    # Calculate the average cross entropy loss across the batch.
    labels = tf.cast(labels, tf.int64)
    cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
        logits, labels, name='cross_entropy_per_example')
    cross_entropy_mean = tf.reduce_mean(cross_entropy, name='cross_entropy')
    tf.add_to_collection('losses', cross_entropy_mean)

    # The total loss is defined as the cross entropy loss plus all of the weight
    # decay terms (L2 loss).
    return tf.add_n(tf.get_collection('losses'), name='total_loss')


def time_error_loss(model_h, model_m, label_h, label_m):
    """
    Compute the time error (in minutes) of the current model.

    Total time difference is expressed in minutes:
       1/N sum( delta(PP, TT))
    where PP and TT are the predicted and true times, expressed in number of
    minutes.
    The delta operator takes care of 'wraparound', so that the difference
    between 9'58 and 10'02 is 4 minutes.

    We also return the individual errors for hours and minutes. Just for fun.

    :param model_h:
    :param model_m:
    :param label_h:
    :param label_m:
    :return: losses for (combined, hours, minutes)
    """

    # Take classifier argmax for most likely hour/minute, and cast everything to
    # float32.
    hours_predicted = tf.cast(tf.argmax(model_h, 1), tf.float32)
    hours_true = tf.cast(label_h, tf.float32)
    minutes_predicted = tf.cast(tf.argmax(model_m, 1), tf.float32)
    minutes_true = tf.cast(label_m, tf.float32)

    delta_time = tf.sub(tf.add(60 * hours_predicted, minutes_predicted),
                        tf.add(60 * hours_true, minutes_true))
    delta_hours = tf.sub(hours_predicted, hours_true)
    delta_minutes = tf.sub(minutes_predicted, minutes_true)

    # TF's mod operator returns negative values:
    #    -7 mod 3 = -1 (we want 2)
    # so we need to do a little extra work.
    def positive_mod(val, div):
        # Return the positive result of the modulo operator.
        # Does x = ((v % div) + div) % div
        return tf.mod(tf.add(tf.mod(val, div), div), div)

    # Handle time wrapping around by comparing the mod of the positive and
    # negative time differences.
    time_error_c = tf.minimum(positive_mod(delta_time, 720),
                              positive_mod(-1 * delta_time, 720))
    time_error_h = tf.minimum(positive_mod(delta_hours, 12.0),
                              positive_mod(-1 * delta_hours, 12.0))
    time_error_m = tf.minimum(positive_mod(delta_minutes, 60.0),
                              positive_mod(-1 * delta_minutes, 60.0))

    avg_error_c = tf.reduce_mean(time_error_c)
    avg_error_h = tf.reduce_mean(time_error_h)
    avg_error_m = tf.reduce_mean(time_error_m)

    return avg_error_c, avg_error_h, avg_error_m


def evaluate_precision(sess, coord, num_records, batch_size, operators):
    """
    Evaluate several operators that compute the precision of the model.

    Returns an array of precisions, one per operator (e.g., hours and minutes)
    and the total number of samples evaluated.

    NOTE: because we run an integer number of batches, the number of evaluated
    samples may be greater than the desired number of samples.

    :param sess: TF session
    :param coord: TF training coordinator.
    :param num_records: Number of records to evaluate.
    :param batch_size: Batch size for evaluating records.
    :param operators: The operators to run
    :return: Precisions array and total sample count.
    """

    # Number of correct predictions: one per operator.
    true_count = [0] * len(operators)

    # Precision rate: one per operator.
    precisions = [0] * len(operators)

    # Run on (at least) complete training set, going through as
    # many batches as necessary.
    num_iter = int(np.ceil(num_records / batch_size))
    total_sample_count = num_iter * batch_size
    batch_num = 0
    while batch_num < num_iter and not coord.should_stop():

        correct_predictions = sess.run(operators)
        for (idx, pred) in enumerate(correct_predictions):
            true_count[idx] += np.sum(pred)

        batch_num += 1

    # Divide by sample count to get true precision.
    for (idx, correct) in enumerate(true_count):
        precisions[idx] = float(correct) / total_sample_count

    return precisions, total_sample_count


def compute_time_predictions(sess, coord, models, labels, num_records, batch_size):
    """
    Compute the time prediction *and* the ground truth time.

    NOTE: because we run an integer number of batches, the number of evaluated
    samples may be greater than the desired number of samples.

    :param sess: TF session
    :param coord: TF training coordinator.
    :param model: The models to evaluate, tuple: (hours, minutes).
    :param label: The true labels, tuple: (hours, minutes).
    :param num_records: Number of records to evaluate.
    :param batch_size: Batch size for evaluating records.
    :return: predicted_times, true_times, sample_count. Each time vector a list
    of tuples with (hour, minute).
    """

    predicted_times = []
    true_times = []

    # Run on (at least) complete training set, going through as
    # many batches as necessary.
    num_iter = int(np.ceil(num_records / batch_size))
    total_sample_count = num_iter * batch_size
    batch_num = 0
    while batch_num < num_iter and not coord.should_stop():

        (out_h, out_m, true_h, true_m) = sess.run(
            [models[0], models[1], labels[0], labels[1]])
        for (hours_dist, minutes_dist, hour_truth, minute_truth) in zip(
                out_h, out_m, true_h, true_m):
            # Find the most likely class.
            hour_predicted = np.argmax(hours_dist)
            minute_predicted = np.argmax(minutes_dist)

            predicted_times.append((hour_predicted, minute_predicted))
            true_times.append((hour_truth, minute_truth))

        batch_num += 1

    return predicted_times, true_times, total_sample_count


def _add_loss_summaries(total_loss):
    """Add summaries for losses.

    Generates moving average for all losses and associated summaries for
    visualizing the performance of the network.

    Args:
      total_loss: Total loss from loss().
    Returns:
      loss_averages_op: op for generating moving averages of losses.
    """
    # Compute the moving average of all individual losses and the total loss.
    loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
    losses = tf.get_collection('losses')
    loss_averages_op = loss_averages.apply(losses + [total_loss])

    # Attach a scalar summary to all individual losses and the total loss; do the
    # same for the averaged version of the losses.
    for l in losses + [total_loss]:
        # Name each loss as '(raw)' and name the moving average version of the loss
        # as the original loss name.
        tf.scalar_summary(l.op.name + ' (raw)', l)
        tf.scalar_summary(l.op.name, loss_averages.average(l))

    return loss_averages_op


def train(total_loss, global_step):
    """ Train the model.

    Create an optimizer and apply to all trainable variables. Add moving
    average for all trainable variables.

    Args:
      total_loss: Total loss from loss().
      global_step: Integer Variable counting the number of training steps
        processed.
    Returns:
      train_op: op for training.
    """
    # Variables that affect learning rate.
    num_batches_per_epoch = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / FLAGS.batch_size
    decay_steps = int(num_batches_per_epoch * NUM_EPOCHS_PER_DECAY)

    # Decay the learning rate exponentially based on the number of steps.
    lr = tf.train.exponential_decay(INITIAL_LEARNING_RATE,
                                    global_step,
                                    decay_steps,
                                    LEARNING_RATE_DECAY_FACTOR,
                                    staircase=True)
    tf.scalar_summary('learning_rate', lr)

    # Generate moving averages of all losses and associated summaries.
    loss_averages_op = _add_loss_summaries(total_loss)

    # Compute gradients.
    with tf.control_dependencies([loss_averages_op]):
        opt = tf.train.GradientDescentOptimizer(lr)
        grads = opt.compute_gradients(total_loss)

    # Apply gradients.
    apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

    # Add histograms for trainable variables.
    for var in tf.trainable_variables():
        tf.histogram_summary(var.op.name, var)

    # Add histograms for gradients.
    for grad, var in grads:
        if grad is not None:
            tf.histogram_summary(var.op.name + '/gradients', grad)

    # Track the moving averages of all trainable variables.
    variable_averages = tf.train.ExponentialMovingAverage(
        MOVING_AVERAGE_DECAY, global_step)
    variables_averages_op = variable_averages.apply(tf.trainable_variables())

    with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
        train_op = tf.no_op(name='train')

    return train_op

# Copyright 2015 The TensorFlow Authors and Felix Duvallet.
# All Rights Reserved.
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
# ==============================================================================