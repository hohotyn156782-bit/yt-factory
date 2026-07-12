#!/usr/bin/env python3
"""CLI YT Factory — автономный завод документалок и Shorts для US YouTube.

Команды:
  doctor                — проверка окружения: ключи, ffmpeg, модель Kokoro, ниши, YT-токен
  build <niche> [topic] — собрать одно видео (long/short по формату ниши)
  run <output> [niche]  — автопилот: output = long | shorts (сборка + публикация/мост)
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import core  # noqa: E402


def doctor() -> None:
    core.load_local_secrets()
    print("── yt-factory doctor ──")
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "PEXELS_API_KEY",
              "PIXABAY_API_KEY", "NVIDIA_API_KEY", "TG_BOT_TOKEN", "TG_QUEUE_BOT_TOKEN"):
        v = core.secret(k, required=False)
        print(f"  {'✅' if v else '—'} {k}" + ("" if v else "  (не задан)"))
    import shutil as sh
    print(f"  {'✅' if sh.which('ffmpeg') else '❌'} ffmpeg")
    kd = core.CACHE_DIR / "kokoro"
    have_model = kd.exists() and any(kd.glob("*.onnx"))
    print(f"  {'✅' if have_model else '⌛'} Kokoro-модель ({kd}){'' if have_model else ' — скачается при первой озвучке'}")
    tok = pathlib.Path(core.secret("YT_TOKEN_FILE", required=False)
                       or "~/.config/yt-factory/yt_token.json").expanduser()
    print(f"  {'✅' if tok.exists() else '—'} YouTube OAuth-токен ({tok})"
          + ("" if tok.exists() else "  — публикация пойдёт через TG-мост"))
    print("  Ниши:")
    for n in core.load_niches(only_enabled=True):
        print(f"    • {n['id']}  format={n.get('format')}  engine={n.get('engine')}  voice={n.get('voice')}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if cmd == "doctor":
        doctor()
    elif cmd == "build":
        if len(sys.argv) < 3:
            raise SystemExit("build <niche> [topic]")
        from pipeline.build import build_video
        res = build_video(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        m = res.get("meta", {})
        print(json.dumps({"video": res.get("video"), "duration": res.get("duration"),
                          "qa_ok": (res.get("qa") or {}).get("ok"),
                          "title": m.get("title"), "publish": res.get("publish")},
                         ensure_ascii=False, indent=1))
    elif cmd == "run":
        if len(sys.argv) < 3:
            raise SystemExit("run <long|shorts> [niche]")
        from pipeline import autopilot
        autopilot.run(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
