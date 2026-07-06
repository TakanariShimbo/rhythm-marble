#!/usr/bin/env python3
"""YourMT3+ で音源を楽器別マルチトラックMIDIに採譜する。

専用venv(vendor/ymt3-venv)で実行する:
  vendor/ymt3-venv/bin/python transcribe_ymt3.py input.wav -o output.mid

モデル: YPTF.MoE+Multi (noPS) — 論文で最高精度の構成
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
YMT3 = ROOT / "vendor" / "YourMT3"
sys.path.insert(0, str(YMT3 / "amt" / "src"))
sys.path.insert(0, str(YMT3))

CHECKPOINT = "mc13_256_g4_all_v7_mt3f_sqr_rms_moe_wf4_n8k2_silu_rope_rp_b36_nops@last.ckpt"
MODEL_ARGS = [
    CHECKPOINT, "-p", "2024", "-tk", "mc13_full_plus_256", "-dec", "multi-t5",
    "-nl", "26", "-enc", "perceiver-tf", "-sqr", "1", "-ff", "moe",
    "-wf", "4", "-nmoe", "8", "-kmoe", "2", "-act", "silu", "-epe", "rope",
    "-rp", "1", "-ac", "spec", "-hop", "300", "-atc", "1", "-pr", "16",
]


def transcribe_bsz(model, audio_info, bsz):
    """model_helper.transcribe()のバッチサイズ可変版(8GB GPUではbsz=8でOOM)。"""
    import torch
    import torchaudio
    from collections import Counter
    from utils.audio import slice_padded_array
    from utils.event2note import merge_zipped_note_events_and_ties_to_notes
    from utils.note2event import mix_notes
    from utils.utils import write_model_output_as_midi

    audio, sr = torchaudio.load(uri=audio_info["filepath"])
    audio = torch.mean(audio, dim=0).unsqueeze(0)
    audio = torchaudio.functional.resample(audio, sr, model.audio_cfg["sample_rate"])
    audio_segments = slice_padded_array(audio, model.audio_cfg["input_frames"],
                                        model.audio_cfg["input_frames"])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    audio_segments = torch.from_numpy(
        audio_segments.astype("float32")).to(device).unsqueeze(1)

    # 8GB GPUに収めるため半精度で推論する
    with torch.no_grad(), torch.autocast(device_type=device.type,
                                         dtype=torch.float16,
                                         enabled=device.type == "cuda"):
        pred_token_arr, _ = model.inference_file(bsz=bsz, audio_segments=audio_segments)

    num_channels = model.task_manager.num_decoding_channels
    n_items = audio_segments.shape[0]
    start_secs_file = [model.audio_cfg["input_frames"] * i / model.audio_cfg["sample_rate"]
                       for i in range(n_items)]
    pred_notes_in_file = []
    n_err_cnt = Counter()
    for ch in range(num_channels):
        pred_token_arr_ch = [arr[:, ch, :] for arr in pred_token_arr]
        zipped_note_events_and_tie, _, ne_err_cnt = \
            model.task_manager.detokenize_list_batches(
                pred_token_arr_ch, start_secs_file, return_events=True)
        pred_notes_ch, n_err_cnt_ch = \
            merge_zipped_note_events_and_ties_to_notes(zipped_note_events_and_tie)
        pred_notes_in_file.append(pred_notes_ch)
        n_err_cnt += n_err_cnt_ch
    pred_notes = mix_notes(pred_notes_in_file)

    write_model_output_as_midi(pred_notes, "./", audio_info["track_name"],
                               model.midi_output_inverse_vocab)
    return os.path.join("./model_output/", audio_info["track_name"] + ".mid")


def main():
    parser = argparse.ArgumentParser(description="YourMT3+で楽器別MIDIに採譜")
    parser.add_argument("input", type=Path, help="入力音源 (wav/mp3)")
    parser.add_argument("-o", "--output", type=Path,
                        help="出力MIDIパス (省略時: <入力名>_ymt3.mid)")
    parser.add_argument("--bsz", type=int, default=4,
                        help="推論バッチサイズ (デフォルト: 4, OOM時は下げる)")
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"エラー: 入力ファイルが見つかりません: {args.input}")
    output = args.output or args.input.with_name(f"{args.input.stem}_ymt3.mid")

    # YourMT3のコードはカレントディレクトリ基準でamt/logsを探すため移動する
    input_abs = args.input.resolve()
    output_abs = output.resolve()
    os.chdir(YMT3)

    import torch
    from model_helper import load_model_checkpoint

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"モデル読み込み中 (device: {device})")
    model = load_model_checkpoint(args=MODEL_ARGS, device="cpu")
    model.to(device)

    audio_info = {
        "filepath": str(input_abs),
        "track_name": input_abs.stem.replace(" ", "_"),
    }
    print(f"採譜中: {input_abs.name} (bsz={args.bsz})")
    midifile = transcribe_bsz(model, audio_info, args.bsz)
    shutil.move(midifile, output_abs)
    print(f"完了: {output_abs}")


if __name__ == "__main__":
    main()
