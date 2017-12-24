import copy
import typing
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from pathlib import Path
from typing import Callable
from typing import Dict
from typing import List

import chainer
import librosa
import numpy
import pysptk
import pyworld

from ..config import DatasetConfig
from ..data_struct import AcousticFeature
from ..data_struct import Wave


class BaseDataProcess(metaclass=ABCMeta):
    @abstractmethod
    def __call__(self, data, test):
        pass


class LambdaProcess(BaseDataProcess):
    def __init__(self, process: Callable[[any, bool], any]):
        self._process = process

    def __call__(self, data, test):
        return self._process(data, test)


class DictKeyReplaceProcess(BaseDataProcess):
    def __init__(self, key_map: Dict[str, str]):
        self._key_map = key_map

    def __call__(self, data: Dict[str, any], test):
        return {key_after: data[key_before] for key_after, key_before in self._key_map}


class ChainProcess(BaseDataProcess):
    def __init__(self, process: typing.Iterable[BaseDataProcess]):
        self._process = list(process)

    def __call__(self, data, test):
        for p in self._process:
            data = p(data, test)
        return data

    def append(self, process: BaseDataProcess):
        self._process.append(process)


class SplitProcess(BaseDataProcess):
    def __init__(self, process: typing.Dict[str, typing.Optional[BaseDataProcess]]):
        self._process = process

    def __call__(self, data, test):
        data = {
            k: p(data, test) if p is not None else data
            for k, p in self._process.items()
        }
        return data


class WaveFileLoadProcess(BaseDataProcess):
    def __init__(self, sample_rate: int, top_db: float, pad_second: float = 0, dtype=numpy.float32):
        self._sample_rate = sample_rate
        self._top_db = top_db
        self._pad_second = pad_second
        self._dtype = dtype

    def __call__(self, data: str, test):
        wave = librosa.core.load(data, sr=self._sample_rate, dtype=self._dtype)[0]
        if self._top_db is not None:
            wave = librosa.effects.remix(wave, intervals=librosa.effects.split(wave, top_db=self._top_db))
        if self._pad_second > 0.0:
            p = int(self._sample_rate * self._pad_second)
            wave = numpy.pad(wave, pad_width=(p, p), mode='constant')
        return Wave(wave, self._sample_rate)


class AcousticFeatureProcess(BaseDataProcess):
    def __init__(self, frame_period, order, alpha, dtype=numpy.float32):
        self._frame_period = frame_period
        self._order = order
        self._alpha = alpha
        self._dtype = dtype

    def __call__(self, data: Wave, test):
        x = data.wave.astype(numpy.float64)
        fs = data.sampling_rate

        _f0, t = pyworld.dio(x, fs, frame_period=self._frame_period)
        f0 = pyworld.stonemask(x, _f0, t, fs)
        spectrogram = pyworld.cheaptrick(x, f0, t, fs)
        aperiodicity = pyworld.d4c(x, f0, t, fs)

        mfcc = pysptk.sp2mc(spectrogram, order=self._order, alpha=self._alpha)
        voiced = ~(f0 == 0)  # type: numpy.ndarray

        feature = AcousticFeature(
            f0=f0[:, None].astype(self._dtype),
            spectrogram=spectrogram.astype(self._dtype),
            aperiodicity=aperiodicity.astype(self._dtype),
            mfcc=mfcc.astype(self._dtype),
            voiced=voiced[:, None],
        )
        feature.validate()
        return feature


class AcousticFeatureLoadProcess(BaseDataProcess):
    def __init__(self, validate=False):
        self._validate = validate

    def __call__(self, path: Path, test=None):
        d = numpy.load(path.expanduser()).item()  # type: dict
        feature = AcousticFeature(
            f0=d['f0'],
            spectrogram=d['spectrogram'],
            aperiodicity=d['aperiodicity'],
            mfcc=d['mfcc'],
            voiced=d['voiced'],
        )
        if self._validate:
            feature.validate()
        return feature


class AcousticFeatureSaveProcess(BaseDataProcess):
    def __init__(self, validate=False, ignore: List[str] = None):
        self._validate = validate
        self._ignore = ignore if ignore is not None else []

    def __call__(self, data: Dict[str, any], test=None):
        path = data['path']  # type: Path
        feature = data['feature']  # type: AcousticFeature
        if self._validate:
            feature.validate()

        d = dict(
            f0=feature.f0,
            spectrogram=feature.spectrogram,
            aperiodicity=feature.aperiodicity,
            mfcc=feature.mfcc,
            voiced=feature.voiced,
        )
        for k in self._ignore:
            assert k in d
            d[k] = numpy.nan

        numpy.save(path.absolute(), d)


class DistillateUsingFeatureProcess(BaseDataProcess):
    def __init__(self, targets: List[str]):
        self._targets = targets

    def __call__(self, feature: AcousticFeature, test=None):
        d = defaultdict(lambda: numpy.nan, **{t: getattr(feature, t) for t in self._targets})
        return AcousticFeature(
            f0=d['f0'],
            spectrogram=d['spectrogram'],
            aperiodicity=d['aperiodicity'],
            mfcc=d['mfcc'],
            voiced=d['voiced'],
        )


class MakeMaskProcess(BaseDataProcess):
    def __init__(self):
        pass

    def __call__(self, feature: AcousticFeature, test=None):
        return AcousticFeature(
            f0=feature.voiced,
            spectrogram=numpy.ones_like(feature.spectrogram, dtype=numpy.bool),
            aperiodicity=numpy.ones_like(feature.aperiodicity, dtype=numpy.bool),
            mfcc=numpy.ones_like(feature.mfcc, dtype=numpy.bool),
            voiced=numpy.ones_like(feature.voiced, dtype=numpy.bool),
        ).astype(numpy.float32)


class AcousticFeatureNormalizeProcess(BaseDataProcess):
    def __init__(self, mean: AcousticFeature, var: AcousticFeature):
        self._mean = mean
        self._var = var

    def __call__(self, data: AcousticFeature, test):
        f0 = (data.f0 - self._mean.f0) / numpy.sqrt(self._var.f0)
        f0[~data.voiced] = 0
        return AcousticFeature(
            f0=f0,
            spectrogram=(data.spectrogram - self._mean.spectrogram) / numpy.sqrt(self._var.spectrogram),
            aperiodicity=(data.aperiodicity - self._mean.aperiodicity) / numpy.sqrt(self._var.aperiodicity),
            mfcc=(data.mfcc - self._mean.mfcc) / numpy.sqrt(self._var.mfcc),
            voiced=data.voiced,
        )


class AcousticFeatureDenormalizeProcess(BaseDataProcess):
    def __init__(self, mean: AcousticFeature, var: AcousticFeature):
        self._mean = mean
        self._var = var

    def __call__(self, data: AcousticFeature, test):
        f0 = data.f0 * numpy.sqrt(self._var.f0) + self._mean.f0
        f0[~data.voiced] = 0
        return AcousticFeature(
            f0=f0,
            spectrogram=data.spectrogram * numpy.sqrt(self._var.spectrogram) + self._mean.spectrogram,
            aperiodicity=data.aperiodicity * numpy.sqrt(self._var.aperiodicity) + self._mean.aperiodicity,
            mfcc=data.mfcc * numpy.sqrt(self._var.mfcc) + self._mean.mfcc,
            voiced=data.voiced,
        )


class EncodeFeatureProcess(BaseDataProcess):
    def __init__(self, targets: List[str]):
        self._targets = targets

    def __call__(self, data: AcousticFeature, test):
        feature = numpy.concatenate([getattr(data, t) for t in self._targets], axis=1)
        feature = feature.T
        return feature


class DecodeFeatureProcess(BaseDataProcess):
    def __init__(self, targets: List[str], sizes: Dict[str, int]):
        assert all(t in sizes for t in targets)
        self._targets = targets
        self._sizes = sizes

    def __call__(self, data: numpy.ndarray, test):
        data = data.T

        lens = [self._sizes[t] for t in self._targets]
        assert data.shape[1] == sum(lens)

        d = defaultdict(lambda: numpy.nan, **{
            t: data[:, bef:aft]
            for t, bef, aft in zip(self._targets, [0] + lens[:-1], lens)
        })
        return AcousticFeature(
            f0=d['f0'],
            spectrogram=d['spectrogram'],
            aperiodicity=d['aperiodicity'],
            mfcc=d['mfcc'],
            voiced=d['voiced'],
        )


class ShapeAlignProcess(BaseDataProcess):
    def __call__(self, data, test):
        data1, data2, data3 = data['input'], data['target'], data['mask']
        m = max(data1.shape[1], data2.shape[1], data3.shape[1])
        data1 = numpy.pad(data1, ((0, 0), (0, m - data1.shape[1])), mode='constant')
        data2 = numpy.pad(data2, ((0, 0), (0, m - data2.shape[1])), mode='constant')
        data3 = numpy.pad(data3, ((0, 0), (0, m - data3.shape[1])), mode='constant')
        data['input'], data['target'], data['mask'] = data1, data2, data3
        return data


class RandomPaddingProcess(BaseDataProcess):
    def __init__(self, min_size: int, time_axis: int = 1):
        assert time_axis == 1
        self._min_size = min_size
        self._time_axis = time_axis

    def __call__(self, datas: Dict[str, any], test=True):
        assert not test

        data, seed = datas['data'], datas['seed']
        random = numpy.random.RandomState(seed)

        if data.shape[self._time_axis] >= self._min_size:
            return data

        pre = random.randint(self._min_size - data.shape[self._time_axis] + 1)
        post = self._min_size - pre
        return numpy.pad(data, ((0, 0), (pre, post)), mode='constant')


class LastPaddingProcess(BaseDataProcess):
    def __init__(self, min_size: int, time_axis: int = 1):
        assert time_axis == 1
        self._min_size = min_size
        self._time_axis = time_axis

    def __call__(self, data: numpy.ndarray, test=None):
        if data.shape[self._time_axis] >= self._min_size:
            return data

        pre = self._min_size - data.shape[self._time_axis]
        return numpy.pad(data, ((0, 0), (pre, 0)), mode='constant')


class RandomCropProcess(BaseDataProcess):
    def __init__(self, crop_size: int, time_axis: int = 1):
        self._crop_size = crop_size
        self._time_axis = time_axis

    def __call__(self, datas: Dict[str, any], test=True):
        assert not test

        data, seed = datas['data'], datas['seed']
        random = numpy.random.RandomState(seed)

        len_time = data.shape[self._time_axis]
        assert len_time >= self._crop_size

        start = random.randint(len_time - self._crop_size + 1)
        return numpy.split(data, [start, start + self._crop_size], axis=self._time_axis)[1]


class FirstCropProcess(BaseDataProcess):
    def __init__(self, crop_size: int, time_axis: int = 1):
        self._crop_size = crop_size
        self._time_axis = time_axis

    def __call__(self, data: numpy.ndarray, test=None):
        return numpy.split(data, [0, self._crop_size], axis=self._time_axis)[1]


class AddNoiseProcess(BaseDataProcess):
    def __init__(self, p_global: float = None, p_local: float = None):
        assert p_global is None or 0 <= p_global
        assert p_local is None or 0 <= p_local
        self._p_global = p_global
        self._p_local = p_local

    def __call__(self, data: numpy.ndarray, test):
        assert not test

        g = numpy.random.randn() * self._p_global
        l = numpy.random.randn(*data.shape).astype(data.dtype) * self._p_local
        return data + g + l


class DataProcessDataset(chainer.dataset.DatasetMixin):
    def __init__(self, data: typing.List, data_process: BaseDataProcess):
        self._data = data
        self._data_process = data_process

    def __len__(self):
        return len(self._data)

    def get_example(self, i):
        return self._data_process(data=self._data[i], test=not chainer.config.train)


def create(config: DatasetConfig):
    import glob
    input_paths = list(sorted([Path(p) for p in glob.glob(str(config.input_glob))]))
    target_paths = list(sorted([Path(p) for p in glob.glob(str(config.target_glob))]))
    assert len(input_paths) == len(target_paths)

    acoustic_feature_load_process = AcousticFeatureLoadProcess()
    input_mean = acoustic_feature_load_process(config.input_mean_path, test=True)
    input_var = acoustic_feature_load_process(config.input_var_path, test=True)
    target_mean = acoustic_feature_load_process(config.target_mean_path, test=True)
    target_var = acoustic_feature_load_process(config.target_var_path, test=True)

    # {input_path, target_path}
    data_process_base = ChainProcess([
        SplitProcess(dict(
            input=ChainProcess([
                LambdaProcess(lambda d, test: d['input_path']),
                acoustic_feature_load_process,
                DistillateUsingFeatureProcess(config.features + ['voiced']),
                AcousticFeatureNormalizeProcess(mean=input_mean, var=input_var),
                EncodeFeatureProcess(config.features),
            ]),
            target=ChainProcess([
                LambdaProcess(lambda d, test: d['target_path']),
                acoustic_feature_load_process,
                DistillateUsingFeatureProcess(config.features + ['voiced']),
                AcousticFeatureNormalizeProcess(mean=target_mean, var=target_var),
                SplitProcess(dict(
                    feature=EncodeFeatureProcess(config.features),
                    mask=ChainProcess([
                        MakeMaskProcess(),
                        EncodeFeatureProcess(config.features),
                    ])
                )),
            ]),
        )),
        LambdaProcess(lambda d, test: dict(input=d['input'], target=d['target']['feature'], mask=d['target']['mask'])),
        ShapeAlignProcess(),
    ])

    data_process_train = copy.deepcopy(data_process_base)

    def add_seed():
        return LambdaProcess(lambda d, test: dict(seed=numpy.random.randint(2 ** 32), **d))

    def padding(s):
        return ChainProcess([
            LambdaProcess(lambda d, test: dict(data=d[s], seed=d['seed'])),
            RandomPaddingProcess(min_size=config.train_crop_size),
        ])

    def crop(s):
        return ChainProcess([
            LambdaProcess(lambda d, test: dict(data=d[s], seed=d['seed'])),
            RandomCropProcess(crop_size=config.train_crop_size),
        ])

    data_process_train.append(ChainProcess([
        add_seed(),
        SplitProcess(dict(input=padding('input'), target=padding('target'), mask=padding('mask'))),
        add_seed(),
        SplitProcess(dict(input=crop('input'), target=crop('target'), mask=crop('mask'))),
    ]))

    # add noise
    data_process_train.append(SplitProcess(dict(
        input=ChainProcess([
            LambdaProcess(lambda d, test: d['input']),
            AddNoiseProcess(p_global=config.input_global_noise, p_local=config.input_local_noise),
        ]),
        target=ChainProcess([
            LambdaProcess(lambda d, test: d['target']),
            AddNoiseProcess(p_global=config.target_global_noise, p_local=config.target_local_noise),
        ]),
        mask=ChainProcess([
            LambdaProcess(lambda d, test: d['mask']),
        ]),
    )))

    data_process_test = data_process_base
    data_process_test.append(SplitProcess(dict(
        input=ChainProcess([
            LambdaProcess(lambda d, test: d['input']),
            LastPaddingProcess(min_size=config.train_crop_size),
            FirstCropProcess(crop_size=config.train_crop_size),
        ]),
        target=ChainProcess([
            LambdaProcess(lambda d, test: d['target']),
            LastPaddingProcess(min_size=config.train_crop_size),
            FirstCropProcess(crop_size=config.train_crop_size),
        ]),
        mask=ChainProcess([
            LambdaProcess(lambda d, test: d['mask']),
            LastPaddingProcess(min_size=config.train_crop_size),
            FirstCropProcess(crop_size=config.train_crop_size),
        ]),
    )))

    num_test = config.num_test
    pairs = [
        dict(input_path=input_path, target_path=target_path)
        for input_path, target_path in zip(input_paths, target_paths)
    ]
    numpy.random.RandomState(config.seed).shuffle(pairs)
    train_paths = pairs[num_test:]
    test_paths = pairs[:num_test]
    train_for_evaluate_paths = train_paths[:num_test]

    return {
        'train': DataProcessDataset(train_paths, data_process_train),
        'test': DataProcessDataset(test_paths, data_process_test),
        'train_eval': DataProcessDataset(train_for_evaluate_paths, data_process_test),
    }
