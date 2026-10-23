# -*- coding: utf-8 -*-
"""把生成出来的 SMC 塞进离线镜像，打开就能听。

    python -m sky15.publish 生成.smc --title 我的曲子

做的事：把谱子写成 offline/scores/<新id>.txt，再往 offline/api/scores_index.json
追加一条，镜像刷新后曲库第一位就是它。
"""
import os
import json, shutil, sys, pathlib

MIRROR = pathlib.Path(os.environ.get("SKY15_MIRROR", ""))
ID_START = 9000


def publish(smc_path, title=None):
    smc_path = pathlib.Path(smc_path)
    text = smc_path.read_text(encoding="utf-8")

    from . import smc as S
    meta = S.parse_meta(text)
    title = title or meta["title"] or smc_path.stem

    # 曲名要写进谱子本身（播放页的标题取自 SMC 的 曲名 字段，
    # 不是取自曲库列表）。不写的话播放页会显示「未命名」。
    if meta["title"] != title:
        import re
        text = re.sub(r"曲名:\[[^\]]*\]", f"曲名:[{title}]", text, count=1)

    idx_path = MIRROR / "api" / "scores_index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else []

    used = {int(x.get("sid", 0)) for x in idx}
    sid = ID_START
    while sid in used:
        sid += 1

    (MIRROR / "scores").mkdir(parents=True, exist_ok=True)
    (MIRROR / "scores" / f"{sid}.txt").write_text(text, encoding="utf-8")

    # seq 是站点的排序键（曲库按 seq DESC, sid DESC 排）。留空的话
    # 前端一排就把新谱甩到最后一页，看起来像"没发上去"——踩过这个坑。
    seq = 0
    for x in idx:
        try:
            seq = max(seq, int(x.get("seq") or 0))
        except (TypeError, ValueError):
            pass
    seq = max(seq, sid) + 1

    keys = sorted({k for x in idx for k in x})
    row = {k: "" for k in keys} if keys else {}
    row.update({
        "sid": sid, "title": title, "seq": seq,
        "cover": row.get("cover", ""),
        "nick": row.get("nick", ""),
        "tempo": meta["tempo"], "timeSig": meta["time_sig"],
        "key": meta["key"], "difficulty": meta["difficulty"],
        "noteCount": sum(1 for g in S.parse_groups(text) for c in g[:3] if c not in "0<> "),
        "free": 1, "nickname": "本地生成",
    })
    idx.insert(0, row)
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")

    print(f"已发布：sid={sid} 《{title}》 → {MIRROR / 'scores' / f'{sid}.txt'}")
    print(f"已写入 {MIRROR / 'scores'}，刷新你自己的谱站页面即可看到")
    return sid


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    title = None
    if "--title" in args:
        i = args.index("--title")
        title = args[i + 1]
        del args[i:i + 2]
    publish(args[0], title)
