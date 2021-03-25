import argparse
import json
from pathlib import Path
import time

import numpy as np
import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()
import pescador

import shared
import data_loaders
import models
import classification_heads


def write_summary(value, tag, step, writer):
    # Create a new Summary object with your measure
    summary = tf.Summary(value=[
        tf.Summary.Value(tag=tag, simple_value=value),
    ])

    writer.add_summary(summary, step)

def tf_define_model_and_cost(config):
    return model_and_cost(config, tf.placeholder(tf.bool))

def tf_define_model_and_cost_freeze(config):
    return model_and_cost(config, False)

def model_and_cost(config, is_train):
    # tensorflow: define the model
    with tf.name_scope('model'):
        x = tf.placeholder(tf.float32, [None, config['xInput'], config['yInput']])
        y_ = tf.placeholder(tf.float32, [None, config['num_classes_dataset']])

        # choose between transfer learning or fully trainable models
        if config['load_model'] is not None:
            import models_transfer_learning
            y = models_transfer_learning.define_model(x, is_train, config)
        else:
            import models
            y = models.model_number(x, is_train, config)

        y = classification_heads.regular(y, config)

        normalized_y = tf.nn.softmax(y)
        print(normalized_y.get_shape())

    print('Number of parameters of the model: ' + str(shared.count_params(tf.trainable_variables())) + '\n')

    # tensorflow: define cost function
    with tf.name_scope('metrics'):
        # if you use softmax_cross_entropy be sure that the output of your model has linear units!
        cost = tf.losses.softmax_cross_entropy(y_, y)
        if config['weight_decay'] is not None:
            vars = tf.trainable_variables()
            lossL2 = tf.add_n([tf.nn.l2_loss(v) for v in vars if 'kernel' or 'weights' in v.name])
            cost = cost + config['weight_decay'] * lossL2
            print('L2 norm, weight decay!')

    # print all trainable variables, for debugging
    model_vars = [v for v in tf.global_variables()]
    for variables in tf.trainable_variables():
        print(variables)

    return [x, y_, is_train, y, normalized_y, cost, model_vars]


if __name__ == '__main__':
    # load config parameters defined in 'config_file.py'
    parser = argparse.ArgumentParser()
    parser.add_argument('config_file', help='configuration file')
    parser.add_argument('-s', '--single_batch', action='store_true', help='iterate over a single batch')
    parser.add_argument('-n', '--number_samples', type=int, help='iterate over a just n random samples')
    args = parser.parse_args()
    config_file = args.config_file
    single_batch = args.single_batch
    number_samples = args.number_samples

    config = json.load(open(config_file, "r"))
    exp_dir = Path(config['exp_dir'])
    data_dir = Path(config['data_dir'])

    # for now on use the subset of parameters for training
    config = config['config_train']

    np.random.seed(seed=config['seed'])

    config['xInput'] = 1
    config['yInput'] = sum([i['n_embeddings'] for i in config['feature_params']])

    # get the data loader
    print('Loading data generator for regular training')

    # load audio representation paths
    file_index = data_dir / 'index_repr.tsv'
    [audio_repr_paths, id2audio_repr_path] = shared.load_id2path(file_index)

    # load training data
    file_ground_truth_train = config['gt_train']
    [ids_train, id2gt_train] = shared.load_id2gt(file_ground_truth_train)

    # load validation data
    file_ground_truth_val = config['gt_val']
    [ids_val, id2gt_val] = shared.load_id2gt(file_ground_truth_val)

    # set output
    config['classes_vector'] = list(range(config['num_classes_dataset']))

    print('# Train:', len(ids_train))
    print('# Val:', len(ids_val))
    print('# Classes:', config['classes_vector'])

    # save experimental settings
    experiment_id = str(shared.get_epoch_time()) + config['feature_params'][0]['type']
    exp_dir_fold = exp_dir / 'experiments' / experiment_id
    exp_dir_fold.mkdir(parents=True, exist_ok=True)

    json.dump(config, open(exp_dir_fold / 'config.json', 'w'))
    print('\nConfig file saved: ' + str(config))

    # tensorflow: define model and cost
    [x, y_, is_train, y, normalized_y, cost, model_vars] = tf_define_model_and_cost(config)

    # tensorflow: define optimizer
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)  # needed for batchnorm
    with tf.control_dependencies(update_ops):
        lr = tf.placeholder(tf.float32)
        if config['optimizer'] == 'SGD_clip':
            optimizer = tf.train.GradientDescentOptimizer(lr)
            gradients, variables = zip(*optimizer.compute_gradients(cost))
            gradients, _ = tf.clip_by_global_norm(gradients, 5.0)
            train_step = optimizer.apply_gradients(zip(gradients, variables))
        elif config['optimizer'] == 'SGD':
            optimizer = tf.train.GradientDescentOptimizer(lr)
            train_step = optimizer.minimize(cost)
        elif config['optimizer'] == 'Adam':
            optimizer = tf.train.AdamOptimizer(learning_rate=lr)
            train_step = optimizer.minimize(cost)

    sess = tf.InteractiveSession()
    tf.keras.backend.set_session(sess)

    print('\nEXPERIMENT: ', str(experiment_id))
    print('-----------------------------------')

    if single_batch:
        print('Iterating over a single batch')

        if number_samples:
            size = number_samples
        else:
            size = config['batch_size']
        np.random.seed(0)
        ids_train = list(np.array(ids_train)[np.random.randint(0, high=len(ids_train), size=size)])
        ids_val = list(np.array(ids_val)[np.random.randint(0, high=len(ids_val), size=size)])

        config['ids_train'] = ids_train
        config['ids_val'] = ids_val

        # Re-dump config with ids
        json.dump(config, open(exp_dir_fold + 'config.json', 'w'))

    if len(config['feature_types']) > 1:
        data_gen = data_loaders.data_gen_multi_feat
    else:
        data_gen = data_loaders.data_gen_standard

    # pescador train: define streamer
    train_pack = [config, config['train_sampling'], config['param_train_sampling']]
    train_streams = [pescador.Streamer(
        data_gen, id, id2audio_repr_path[id], id2gt_train[id], train_pack) for id in ids_train]
    train_mux_stream = pescador.StochasticMux(
        train_streams, n_active=config['batch_size'] * 2, rate=None, mode='exhaustive')
    train_batch_streamer = pescador.Streamer(
        pescador.buffer_stream, train_mux_stream, buffer_size=config['batch_size'], partial=True)
    train_batch_streamer = pescador.ZMQStreamer(train_batch_streamer)

    # pescador val: define streamer
    val_pack = [config, 'overlap_sampling', config['xInput']]
    val_streams = [pescador.Streamer(
        data_gen, id, id2audio_repr_path[id], id2gt_val[id], val_pack) for id in ids_val]
    val_mux_stream = pescador.ChainMux(val_streams, mode='exhaustive')
    val_batch_streamer = pescador.Streamer(
        pescador.buffer_stream, val_mux_stream, buffer_size=config['val_batch_size'], partial=True)
    val_batch_streamer = pescador.ZMQStreamer(val_batch_streamer)

    update_on_train = True

    train_file_writer = tf.summary.FileWriter(exp_dir_fold / 'logs' / 'train', sess.graph)
    val_file_writer = tf.summary.FileWriter(exp_dir_fold / 'logs' / 'val', sess.graph)

    # tensorflow: create a session to run the tensorflow graph
    sess.run(tf.global_variables_initializer())
    # Required by the accuracy metrics
    sess.run(tf.local_variables_initializer())
    saver = tf.train.Saver()

    # After restoring make it aware of the rest of the variables
    # saver.var_list = model_vars
    saver = tf.train.Saver()

    # writing headers of the train_log.tsv
    fy = open(exp_dir_fold / 'train_log.tsv', 'a')
    fy.write('Epoch\ttrain_cost\tval_cost\tepoch_time\tlearing_rate\n')

    fy.close()

    # automate the evaluation process
    experiment_id_file = exp_dir / f"experiment_id_{config['fold']}"
    with open(experiment_id_file, 'w') as f:
        f.write(str(experiment_id))

    # training
    k_patience = 0
    cost_best_model = np.Inf
    tmp_learning_rate = config['learning_rate']
    print('Training started..')

    for i in range(config['epochs']):
        # training: do not train first epoch, to see random weights behaviour
        i, train_batch_streamer, sess, train_step, cost
        start_time = time.time()
        array_train_cost = []
        if i != 0:
            for train_batch in train_batch_streamer:
                tf_start = time.time()
                _, train_cost = sess.run([train_step, cost],
                                         feed_dict={x: train_batch['X'],
                                                    y_: train_batch['Y'],
                                                    lr: tmp_learning_rate,
                                                    is_train: True}
                                         )
                array_train_cost.append(train_cost)

        # validation
        array_val_cost = []
        for val_batch in val_batch_streamer:
            val_cost = sess.run([cost],
                                feed_dict={x: val_batch['X'],
                                           y_: val_batch['Y'],
                                           is_train: False}
                                )
            array_val_cost.append(val_cost)

        # Keep track of average loss of the epoch
        train_cost = np.mean(array_train_cost)

        val_cost = np.mean(array_val_cost)
        epoch_time = time.time() - start_time
        fy = open(exp_dir_fold / 'train_log.tsv', 'a')
        fy.write('%g\t%g\t%g\t%gs\t%g\n' % (i + 1, train_cost, val_cost, epoch_time, tmp_learning_rate))
        fy.close()

        # Decrease the learning rate after not improving in the validation set
        if config['patience'] and k_patience >= config['patience']:
            print('Changing learning rate!')
            tmp_learning_rate = tmp_learning_rate / 2
            print(tmp_learning_rate)
            k_patience = 0

        # Early stopping: keep the best model in validation set
        if val_cost >= cost_best_model:
            k_patience += 1
            print('Epoch %d, train cost %g, '
                  'val cost %g, '
                  'epoch-time %gs, lr %g, time-stamp %s' %
                  (i + 1, train_cost, val_cost, epoch_time, tmp_learning_rate,
                   str(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))))

        else:
            # save model weights to disk
            save_path = saver.save(sess, str(exp_dir_fold) + '/')
            print('Epoch %d, train cost %g, '
                  'val cost %g, '
                  'epoch-time %gs, lr %g, time-stamp %s - [BEST MODEL]'
                  ' saved in: %s' %
                  (i + 1, train_cost, val_cost, epoch_time, tmp_learning_rate,
                   str(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())), save_path))
            cost_best_model = val_cost

    print('\nEVALUATE EXPERIMENT -> ' + str(experiment_id))
