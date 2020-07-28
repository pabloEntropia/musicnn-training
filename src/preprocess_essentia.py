import os
from joblib import Parallel, delayed
import json
import argparse
import pickle
import numpy as np
from pathlib import Path
import yaml
from argparse import Namespace
from essentia.pytools.extractors.melspectrogram import melspectrogram
from tqdm import tqdm

config_file = Namespace(**yaml.load(open('config_file.yaml'), Loader=yaml.SafeLoader))

DEBUG = False


def compute_audio_repr(audio_file, audio_repr_file, force=False):
    if not force:
        if os.path.exists(audio_repr_file):
            print('{} exists. skipping!'.format(audio_file))
            return 0

    if config['type'] == 'waveform':
        audio, sr = librosa.load(audio_file, sr=config['resample_sr'])
        audio_repr = audio
        audio_repr = np.expand_dims(audio_repr, axis=1)

    elif config['spectrogram_type'] == 'mel':
        audio_repr = melspectrogram(audio_file,
                                    sample_rate=config['resample_sr'],
                                    frame_size=config['n_fft'],
                                    hop_size=config['hop'],
                                    window_type='hann',
                                    low_frequency_bound=0,
                                    high_frequency_bound=config['resample_sr'] / 2,
                                    number_bands=config['n_mels'],
                                    warping_formula='slaneyMel',
                                    weighting='linear',
                                    normalize='unit_tri',
                                    bands_type='magnitude',
                                    compression_type='none')

    # Compute length
    length = audio_repr.shape[0]

    # Transform to float16 (to save storage, and works the same)
    audio_repr = audio_repr.astype(np.float16)

    # Write results:
    with open(audio_repr_file, "wb") as f:
        pickle.dump(audio_repr, f)  # audio_repr shape: NxM

    return length


def do_process(files, index):
    try:
        [id, audio_file, audio_repr_file] = files[index]
        if not os.path.exists(audio_repr_file[:audio_repr_file.rfind('/') + 1]):
            path = Path(audio_repr_file[:audio_repr_file.rfind('/') + 1])
            path.mkdir(parents=True, exist_ok=True)
        # compute audio representation (pre-processing)
        length = compute_audio_repr(audio_file, audio_repr_file)
        # index.tsv writing
        fw = open(config_file.DATA_FOLDER + config['audio_representation_folder'] + "index_" + str(config['machine_i']) + ".tsv", "a")
        fw.write("%s\t%s\t%s\n" % (id, audio_repr_file[len(config_file.DATA_FOLDER):], audio_file[len(config_file.DATA_FOLDER):]))
        fw.close()
        print(str(index) + '/' + str(len(files)) + ' Computed: %s' % audio_file)

    except Exception as e:
        ferrors = open(config_file.DATA_FOLDER + config['audio_representation_folder'] + "errors" + str(config['machine_i']) + ".txt", "a")
        ferrors.write(audio_file + "\n")
        ferrors.write(str(e))
        ferrors.close()
        print('Error computing audio representation: ', audio_file)
        print(str(e))


def process_files(files):

    if DEBUG:
        print('WARNING: Parallelization is not used!')
        for index in tqdm(range(0, len(files))):
            do_process(files, index)

    else:
        Parallel(n_jobs=config['num_processing_units'])(
            delayed(do_process)(files, index) for index in range(0, len(files)))


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('configurationID', help='ID of the configuration dictionary')
    args = parser.parse_args()
    config = config_file.config_preprocess[args.configurationID]

    # set audio representations folder
    if not os.path.exists(config_file.DATA_FOLDER + config['audio_representation_folder']):
        os.makedirs(config_file.DATA_FOLDER + config['audio_representation_folder'])
    else:
        print("WARNING: already exists a folder with this name!"
              "\nThis is expected if you are splitting computations into different machines.."
              "\n..because all these machines are writing to this folder. Otherwise, check your config_file!")

    # list audios to process: according to 'index_file'
    files_to_convert = []
    f = open(config_file.DATA_FOLDER + config["index_audio_file"])
    for line in f.readlines():
        id, audio = line.strip().split("\t")
        audio_repr = audio[:audio.rfind(".")] + ".pk" # .npy or .pk
        files_to_convert.append((id, config['audio_folder'] + audio,
                                 config_file.DATA_FOLDER + config['audio_representation_folder'] + audio_repr))

    # compute audio representation
    if config['machine_i'] == config['n_machines'] - 1:
        process_files(files_to_convert[int(len(files_to_convert) / config['n_machines']) * (config['machine_i']):])
        # we just save parameters once! In the last thread run by n_machine-1!
        json.dump(config, open(config_file.DATA_FOLDER + config['audio_representation_folder'] + "config.json", "w"))
    else:
        first_index = int(len(files_to_convert) / config['n_machines']) * (config['machine_i'])
        second_index = int(len(files_to_convert) / config['n_machines']) * (config['machine_i'] + 1)
        assigned_files = files_to_convert[first_index:second_index]
        process_files(assigned_files)

    print("Audio representation folder: " + config_file.DATA_FOLDER + config['audio_representation_folder'])
