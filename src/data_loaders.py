import numpy as np
import random
import os
from pathlib import Path


def compress(audio_rep, compression=None):
    # do not apply any compression to the embeddings
    if not compression:
        return audio_rep
    elif compression == 'logEPS':
        return np.log10(audio_rep + np.finfo(float).eps)
    elif compression == 'logC':
        return np.log10(10000 * audio_rep + 1)
    else:
        raise('get_audio_rep: Preprocessing not available.')


def get_short_rep(audio_repr_path, x, y, frames_num):
    fp = np.memmap(audio_repr_path, dtype='float16',
                   mode='r', shape=(frames_num, y))
    audio_rep = np.zeros([x, y])
    audio_rep[:frames_num, :] = np.array(fp)
    del fp

    return audio_rep


def read_mmap(audio_repr_path, x, y, frames_num, single_patch=False, offset=0, compression=None):
    if frames_num < x:
        audio_repr = get_short_rep(audio_repr_path, x, y, frames_num)
    else:
        read_x = x if single_patch else frames_num
        fp = np.memmap(audio_repr_path, dtype='float16',
                       mode='r', shape=(read_x, y), offset=offset)
        audio_repr = np.array(fp)
        del fp
    return compress(audio_repr, compression=compression)


def data_gen_standard(id, audio_repr_path, gt, pack):
    # Support both the absolute and relative path input cases
    config, sampling, param_sampling = pack
    audio_repr_path = Path(config['audio_representation_dirs'][0], audio_repr_path)

    try:
        floats_num = os.path.getsize(audio_repr_path) // 2  # each float16 has 2 bytes
        frames_num = floats_num // config['yInput']

        # let's deliver some data!
        if sampling == 'random':
            for i in range(0, param_sampling):
                random_frame_offset = random.randint(
                    0, frames_num - config['xInput'])
                # idx * bands * bytes per float
                offset = random_frame_offset * config['yInput'] * 2
                yield {
                    'X': read_mmap(audio_repr_path,
                                   config['xInput'],
                                   config['yInput'],
                                   frames_num,
                                   single_patch=True,
                                   offset=offset),
                    'Y': gt,
                    'ID': id
                }

        elif sampling == 'overlap_sampling':
            audio_rep = read_mmap(audio_repr_path, config['xInput'], config['yInput'], frames_num)
            last_frame = int(audio_rep.shape[0]) - int(config['xInput']) + 1
            for time_stamp in range(0, last_frame, param_sampling):
                yield {
                    'X': audio_rep[time_stamp: time_stamp + config['xInput'], :],
                    'Y': gt,
                    'ID': id
                }
    except Exception as ex:
        print('"{}" failed'.format(audio_repr_path))
        print(repr(ex))


def data_gen_multi_feat(id, audio_repr_path, gt, pack):
    # Support both the absolute and relative path input cases
    config, sampling, param_sampling = pack

    try:
        frames_nums = []
        for i in range(len(config['feature_params'])):
            n_embeddings = config['feature_params'][i]['n_embeddings']
            float_num = Path(config['audio_representation_dirs'][i], audio_repr_path).stat().st_size // 2  # each float16 has 2 bytes
            frames_nums.append(float_num // n_embeddings)
        frames_num = min(frames_nums)

        # let's deliver some data!
        if sampling == 'random':
            for i in range(0, param_sampling):
                random_frame_offset = random.randint(
                    0, frames_num - config['xInput'])
                # idx * bands * bytes per float

                audio_repr = []
                for j, audio_folder in enumerate(config['audio_representation_dirs']):
                    offset = random_frame_offset * \
                        config['feature_params'][j]['n_embeddings'] * 2

                    if config['feature_types'][j] == 'yamnet':
                        offset *= 2
                    audio_repr.append(read_mmap(Path(audio_folder, audio_repr_path),
                                                config['xInput'],
                                                config['feature_params'][j]['n_embeddings'],
                                                frames_num,
                                                single_patch=True,
                                                offset=offset
                                                )
                                      )
                yield {'X': np.hstack(audio_repr), 'Y': gt, 'ID': id}

        elif sampling == 'overlap_sampling':
            audio_repr = []
            for j, audio_folder in enumerate(config['audio_representation_dirs']):
                if config['feature_types'][j] == 'yamnet':
                    frame_num_ñapa = frames_num * 2
                else:
                    frame_num_ñapa = frames_num
                embedding = read_mmap(Path(audio_folder, audio_repr_path),
                                      config['xInput'],
                                      config['feature_params'][j]['n_embeddings'],
                                      frame_num_ñapa,
                                      compression=None
                                      )

                # ñapa muy temporal!
                if config['feature_types'][j] == 'yamnet':
                    embedding = embedding[::2, :]

                audio_repr.append(embedding)

            audio_repr = np.hstack(audio_repr)
            last_frame = int(audio_repr.shape[0]) - int(config['xInput']) + 1
            for time_stamp in range(0, last_frame, param_sampling):
                yield {
                    'X': audio_repr[time_stamp: time_stamp + config['xInput'], :],
                    'Y': gt,
                    'ID': id
                }
    except Exception as ex:
        print('"{}" failed'.format(audio_repr_path))
        print(repr(ex))
