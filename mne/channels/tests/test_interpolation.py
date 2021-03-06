import os.path as op
import warnings

import numpy as np
from numpy.testing import (assert_allclose, assert_array_equal)
import pytest

from mne import io, pick_types, pick_channels, read_events, Epochs
from mne.channels.interpolation import _make_interpolation_matrix
from mne.utils import run_tests_if_main

base_dir = op.join(op.dirname(__file__), '..', '..', 'io', 'tests', 'data')
raw_fname = op.join(base_dir, 'test_raw.fif')
event_name = op.join(base_dir, 'test-eve.fif')

event_id, tmin, tmax = 1, -0.2, 0.5
event_id_2 = 2


def _load_data():
    """Load data."""
    # It is more memory efficient to load data in a separate
    # function so it's loaded on-demand
    raw = io.read_raw_fif(raw_fname)
    events = read_events(event_name)
    picks_eeg = pick_types(raw.info, meg=False, eeg=True, exclude=[])
    # select every second channel for faster speed but compensate by using
    # mode='accurate'.
    picks_meg = pick_types(raw.info, meg=True, eeg=False, exclude=[])[1::2]
    picks = pick_types(raw.info, meg=True, eeg=True, exclude=[])

    with warnings.catch_warnings(record=True):  # proj
        epochs_eeg = Epochs(raw, events, event_id, tmin, tmax, picks=picks_eeg,
                            preload=True, reject=dict(eeg=80e-6))
        epochs_meg = Epochs(raw, events, event_id, tmin, tmax, picks=picks_meg,
                            preload=True,
                            reject=dict(grad=1000e-12, mag=4e-12))
        epochs = Epochs(raw, events, event_id, tmin, tmax, picks=picks,
                        preload=True, reject=dict(eeg=80e-6, grad=1000e-12,
                                                  mag=4e-12))
    return raw, epochs, epochs_eeg, epochs_meg


@pytest.mark.slowtest
def test_interpolation():
    """Test interpolation."""
    raw, epochs, epochs_eeg, epochs_meg = _load_data()

    # It's a trade of between speed and accuracy. If every second channel is
    # selected the tests are more than 3x faster but the correlation
    # drops to 0.8
    thresh = 0.80

    # check that interpolation does nothing if no bads are marked
    epochs_eeg.info['bads'] = []
    evoked_eeg = epochs_eeg.average()
    with warnings.catch_warnings(record=True) as w:
        evoked_eeg.interpolate_bads()
    assert any('Doing nothing' in str(ww.message) for ww in w)

    # create good and bad channels for EEG
    epochs_eeg.info['bads'] = []
    goods_idx = np.ones(len(epochs_eeg.ch_names), dtype=bool)
    goods_idx[epochs_eeg.ch_names.index('EEG 012')] = False
    bads_idx = ~goods_idx

    evoked_eeg = epochs_eeg.average()
    ave_before = evoked_eeg.data[bads_idx]

    # interpolate bad channels for EEG
    pos = epochs_eeg._get_channel_positions()
    pos_good = pos[goods_idx]
    pos_bad = pos[bads_idx]
    interpolation = _make_interpolation_matrix(pos_good, pos_bad)
    assert interpolation.shape == (1, len(epochs_eeg.ch_names) - 1)
    ave_after = np.dot(interpolation, evoked_eeg.data[goods_idx])

    epochs_eeg.info['bads'] = ['EEG 012']
    evoked_eeg = epochs_eeg.average()
    assert_array_equal(ave_after, evoked_eeg.interpolate_bads().data[bads_idx])

    assert_allclose(ave_before, ave_after, atol=2e-6)

    # check that interpolation fails when preload is False
    epochs_eeg.preload = False
    pytest.raises(RuntimeError, epochs_eeg.interpolate_bads)
    epochs_eeg.preload = True

    # check that interpolation changes the data in raw
    raw_eeg = io.RawArray(data=epochs_eeg._data[0], info=epochs_eeg.info)
    raw_before = raw_eeg._data[bads_idx]
    raw_after = raw_eeg.interpolate_bads()._data[bads_idx]
    assert not np.all(raw_before == raw_after)

    # check that interpolation fails when preload is False
    for inst in [raw, epochs]:
        assert hasattr(inst, 'preload')
        inst.preload = False
        inst.info['bads'] = [inst.ch_names[1]]
        pytest.raises(RuntimeError, inst.interpolate_bads)

    # check that interpolation works with few channels
    raw_few = raw.copy().crop(0, 0.1).load_data()
    raw_few.pick_channels(raw_few.ch_names[:1] + raw_few.ch_names[3:4])
    assert len(raw_few.ch_names) == 2
    raw_few.del_proj()
    raw_few.info['bads'] = [raw_few.ch_names[-1]]
    orig_data = raw_few[1][0]
    with warnings.catch_warnings(record=True) as w:
        raw_few.interpolate_bads(reset_bads=False)
    assert len(w) == 0
    new_data = raw_few[1][0]
    assert (new_data == 0).mean() < 0.5
    assert np.corrcoef(new_data, orig_data)[0, 1] > 0.1

    # check that interpolation works when non M/EEG channels are present
    # before MEG channels
    with warnings.catch_warnings(record=True):  # change of units
        raw.rename_channels({'MEG 0113': 'TRIGGER'})
        raw.set_channel_types({'TRIGGER': 'stim'})
        raw.info['bads'] = [raw.info['ch_names'][1]]
        raw.load_data()
        raw.interpolate_bads()

    # check that interpolation works for MEG
    epochs_meg.info['bads'] = ['MEG 0141']
    evoked = epochs_meg.average()
    pick = pick_channels(epochs_meg.info['ch_names'], epochs_meg.info['bads'])

    # MEG -- raw
    raw_meg = io.RawArray(data=epochs_meg._data[0], info=epochs_meg.info)
    raw_meg.info['bads'] = ['MEG 0141']
    data1 = raw_meg[pick, :][0][0]

    raw_meg.info.normalize_proj()
    data2 = raw_meg.interpolate_bads(reset_bads=False)[pick, :][0][0]
    assert np.corrcoef(data1, data2)[0, 1] > thresh
    # the same number of bads as before
    assert len(raw_meg.info['bads']) == len(raw_meg.info['bads'])

    # MEG -- epochs
    data1 = epochs_meg.get_data()[:, pick, :].ravel()
    epochs_meg.info.normalize_proj()
    epochs_meg.interpolate_bads()
    data2 = epochs_meg.get_data()[:, pick, :].ravel()
    assert np.corrcoef(data1, data2)[0, 1] > thresh
    assert len(epochs_meg.info['bads']) == 0

    # MEG -- evoked
    data1 = evoked.data[pick]
    evoked.info.normalize_proj()
    data2 = evoked.interpolate_bads().data[pick]
    assert np.corrcoef(data1, data2)[0, 1] > thresh


run_tests_if_main()
