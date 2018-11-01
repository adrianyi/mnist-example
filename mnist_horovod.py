import os
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

import numpy as np
import tensorflow as tf
import horovod.tensorflow as hvd
from clusterone import get_data_path, get_logs_path

from tensorflow.examples.tutorials.mnist.input_data import read_data_sets

tf.logging.set_verbosity(tf.logging.INFO)
try:
    print(os.environ)
    job_name = os.environ['JOB_NAME']
    task_index = int(os.environ['TASK_INDEX'])
    ps_hosts = os.environ['PS_HOSTS'].split(',')
    worker_hosts = os.environ['WORKER_HOSTS'].split(',')
except KeyError as ex:
    job_name = None
    task_index = 0
    ps_hosts = []
    worker_hosts = []
print('job name:', job_name)
print('task_index:', task_index)
print('ps_hosts:', ps_hosts)
print('worker_hosts:', worker_hosts)


def str2bool(v):
    """"""
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise IOError('Boolean value expected (i.e. yes/no, true/false, y/n, t/f, 1/0).')


def get_args():
    """Return parsed args"""
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument('--local_data_dir', type=str, default='data/',
                        help='Path to local data directory')
    parser.add_argument('--local_log_dir', type=str, default='logs/',
                        help='Path to local log directory')
    parser.add_argument('--fashion', type=str2bool, default=False,
                        help='Use Fashion MNIST data')

    # Model params
    parser.add_argument('--hidden_units', type=int, nargs='*', default=[32, 64])
    parser.add_argument('--learning_rate', type=float, default=0.001)
    parser.add_argument('--learning_decay', type=float, default=0.05)
    parser.add_argument('--dropout', type=float, default=0.1)
    parser.add_argument('--batch_size', type=int, default=512)

    # Training params
    parser.add_argument('--train_steps', type=int, default=1e6)
    parser.add_argument('--eval_steps', type=int, default=1000)

    # Debugger
    parser.add_argument('--profiler', type=str2bool, default=False,
                        help='Caution: This will cause training to be slower.')

    opts = parser.parse_args()

    opts.data_dir = get_data_path(dataset_name='adrianyi/mnist-data',
                                  local_root=opts.local_data_dir,
                                  local_repo='',
                                  path='')
    opts.log_dir = get_logs_path(root=opts.local_log_dir)

    return opts


def cnn_net(input_tensor, opts):
    """Return logits output from CNN net"""
    temp = tf.reshape(input_tensor, shape=(-1, 28, 28, 1), name='input_image')
    for i, n_units in enumerate(opts.hidden_units):
        temp = tf.layers.conv2d(temp, filters=n_units, kernel_size=(3, 3), strides=(2, 2),
                                activation=tf.nn.relu, name='cnn'+str(i))
        temp = tf.layers.dropout(temp, rate=opts.dropout)
    temp = tf.reduce_mean(temp, axis=(2, 3), keepdims=False, name='average')
    return tf.layers.dense(temp, 10)


def main(opts):
    # Initiate Horovod
    hvd.init()
    print('Horovod size:', hvd.size())
    print('Horovod local rank:', hvd.local_rank())
    print('Horovod rank:', hvd.rank())
    is_chief = (hvd.rank() == 0)

    if opts.fashion:
        data = read_data_sets(opts.data_dir,
                              source_url='http://fashion-mnist.s3-website.eu-central-1.amazonaws.com/')
    else:
        data = read_data_sets(opts.data_dir)

    def model_fn(features, labels, mode):
        logits = cnn_net(features, opts)
        cent = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=labels, logits=logits, name='cross_entropy')
        loss = tf.reduce_mean(cent, name='loss')
        lr = tf.train.exponential_decay(learning_rate=opts.learning_rate*hvd.size(),
                                        global_step=tf.train.get_global_step(),
                                        decay_steps=1000,
                                        decay_rate=1.-opts.learning_decay)
        optimizer = tf.train.AdamOptimizer(learning_rate=lr)
        # Horovod distributed optimizer
        optimizer = hvd.DistributedOptimizer(optimizer)
        train_op = optimizer.minimize(loss, global_step=tf.train.get_or_create_global_step())

        pred = tf.cast(tf.argmax(logits, axis=1), tf.int64)

        metrics = {'accuracy': tf.metrics.accuracy(labels=labels, predictions=pred, name='accuracy')}

        return tf.estimator.EstimatorSpec(mode=mode,
                                          predictions=pred,
                                          train_op=train_op,
                                          loss=loss,
                                          eval_metric_ops=metrics)

    session_config = tf.ConfigProto()
    session_config.gpu_options.allow_growth = True
    session_config.gpu_options.visible_device_list = str(hvd.local_rank())

    runconfig = tf.estimator.RunConfig(
        model_dir=(opts.log_dir if is_chief else None),
        save_summary_steps=(25 if is_chief else None),
        save_checkpoints_steps=25,
        keep_checkpoint_max=2,
        log_step_count_steps=(10 if is_chief else None),
        session_config=session_config)
    estimator = tf.estimator.Estimator(
        model_dir=(opts.log_dir if is_chief else None),
        model_fn=model_fn,
        config=runconfig)
    bcast_hook = hvd.BroadcastGlobalVariablesHook(0)

    train_input_fn = tf.estimator.inputs.numpy_input_fn(
                         x=data.train.images,
                         y=data.train.labels.astype(np.int32),
                         num_epochs=None,
                         batch_size=opts.batch_size,
                         shuffle=True,
                         queue_capacity=4*opts.batch_size,
                         num_threads=4)
    eval_input_fn = tf.estimator.inputs.numpy_input_fn(
                         x=data.test.images,
                         y=data.test.labels.astype(np.int32),
                         num_epochs=1,
                         shuffle=False)

    training_hooks = [bcast_hook]
    if opts.profiler:
        if job_name is None:
            profiler_logdir = os.path.join(opts.log_dir, 'profiler')
        else:
            profiler_logdir = 'profiler-{}{}'.format(job_name, task_index)
        print('Saving profiler files to {}'.format(profiler_logdir))
        training_hooks.append(tf.train.ProfilerHook(save_secs=10, show_memory=True, output_dir=profiler_logdir))

    while True:
        estimator.train(
            input_fn=train_input_fn,
            steps=opts.eval_steps // hvd.size(),
            hooks=training_hooks)

        estimator.evaluate(
            input_fn=eval_input_fn)


if __name__ == '__main__':
    main(get_args())
