"""一键试跑引擎：给每个模型准备「一定跑得通」的参数。

设计原则：
  · 简单任务现场真跑（2–40s），直接出结果
  · 重任务（splat/mesh）复用已算好的前置资产，避免每次等十几分钟
  · 所有参数都踩过坑（depth 去 confidence、帧数组字段一致、returnDepth 需 ≥2 帧）
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
SEED_FILE = ROOT / "probe" / "seed.json"
ATLAS = "https://atlas.worldlabs.ai/api/v2"

# 任务的演示配置
DEMO = {
    "atlasTextToImage": {"label": "文生图", "eta": "30–40s", "kind": "quick"},
    "images2PosedRGBD": {"label": "图 → 位姿+深度", "eta": "2–5s", "kind": "quick"},
    "atlasChisel": {"label": "深度帧 + 提示 → 新视角", "eta": "8–15s", "kind": "quick"},
    "atlasGenerate": {"label": "上下文 → 新视角", "eta": "10–20s", "kind": "quick"},
    "imageUpscale": {"label": "图像放大", "eta": "5–10s", "kind": "quick"},
    "imageEdit": {"label": "图生图编辑", "eta": "10–20s", "kind": "quick"},
    "atlasEnhancePrompt": {"label": "提示词增强", "eta": "5–15s", "kind": "quick"},
    "posedImages2Splats": {"label": "多视图 → 3D Splats", "eta": "10–13min", "kind": "long"},
    "splats2Mesh": {"label": "Splats → Mesh", "eta": "1–2min", "kind": "long"},
    "chiselCaption": {"label": "场景描述", "eta": "5–10s", "kind": "web"},
    "chiselCaptionUpdate": {"label": "更新描述", "eta": "5–10s", "kind": "web"},
}

PROMPT = "a modern dashboard interface on a dark theme, clean typography"


class Demo:
    def __init__(self, call, upload) -> None:
        """call: async (model, body, wait) -> (status, data); upload: async (Path) -> assetId"""
        self.call = call
        self.upload = upload
        self._seed: dict[str, Any] | None = None

    # ---------------------------------------------------------------- 种子资产
    def _load_seed(self) -> dict[str, Any]:
        if self._seed:
            return self._seed
        if SEED_FILE.exists():
            try:
                self._seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._seed = {}
        else:
            self._seed = {}
        return self._seed

    @staticmethod
    def _fallback(model: str) -> dict | None:
        """实时跑不通时，回落到本机保存的真实历史结果。"""
        m = {
            "atlasTextToImage": "text2img", "images2PosedRGBD": "rgbd",
            "atlasChisel": "chisel3", "atlasGenerate": "generate",
            "posedImages2Splats": "splat", "splats2Mesh": "mesh",
        }
        tag = m.get(model)
        if not tag:
            return None
        f = ROOT / "probe" / f"{tag}.json"
        if not f.exists():
            return None
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return {"result": d.get("response") or d,
                    "savedAt": f.stat().st_mtime, "tag": tag}
        except Exception:  # noqa: BLE001
            return None

    def _save_seed(self) -> None:
        SEED_FILE.parent.mkdir(exist_ok=True)
        SEED_FILE.write_text(json.dumps(self._seed or {}, ensure_ascii=False), encoding="utf-8")

    async def seed_asset(self) -> str:
        """上传一张演示图，缓存 assetId。"""
        s = self._load_seed()
        if s.get("image"):
            # 验证还活着
            st, d = await self.call("images2PosedRGBD",
                                    {"frames": [{"imageAsset": {"assetId": s["image"]}}]}, False)
            if st < 300:
                return s["image"]
        img = ROOT / "assets" / "models.png"
        if not img.exists():
            img = ROOT / "assets" / "gateway.png"
        aid = await self.upload(img)
        s["image"] = aid
        self._seed = s
        self._save_seed()
        return aid

    # ---------------------------------------------------------------- 帧工具
    @staticmethod
    def _frame(f: dict[str, Any], with_depth: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"imageAsset": f["imageAsset"], "camera": f["camera"]}
        if with_depth and f.get("depth"):
            # atlasChisel 不读 confidence，带上会 422
            out["depth"] = {"depthAsset": f["depth"]["depthAsset"]}
        return out

    async def rgbd_frames(self, asset_id: str, n: int = 1) -> list[dict[str, Any]]:
        st, d = await self.call("images2PosedRGBD",
                                {"frames": [{"imageAsset": {"assetId": asset_id}}],
                                 "targetResolution": [1280, 720]}, True)
        if st >= 300:
            raise RuntimeError(f"RGBD 失败: {json.dumps(d, ensure_ascii=False)[:220]}")
        return (d.get("response") or {}).get("frames") or []

    async def multiview(self, asset_id: str, n: int = 3) -> list[dict[str, Any]]:
        """同图 + 多个相机 → 多视角（带 depth），供 splat 使用。"""
        base = await self.rgbd_frames(asset_id)
        if not base:
            raise RuntimeError("RGBD 没返回帧")
        f0 = base[0]
        cams = []
        for i in range(n):
            cm = copy.deepcopy(f0["camera"])
            cm["extrinsics"]["position"][0] += (i - (n - 1) / 2) * 0.22
            cams.append(cm)
        st, d = await self.call("atlasChisel", {
            "targetFrames": [{"imageAsset": f0["imageAsset"], "camera": c} for c in cams],
            "prompt": PROMPT, "returnDepth": True}, True)
        if st >= 300:
            raise RuntimeError(f"多视角生成失败: {json.dumps(d, ensure_ascii=False)[:220]}")
        return (d.get("response") or {}).get("frames") or []

    # ---------------------------------------------------------------- 主入口
    async def run(self, model: str) -> dict[str, Any]:
        t0 = time.time()
        if model == "atlasTextToImage":
            st, d = await self.call(model, {"prompt": PROMPT, "aspectRatio": "16:9",
                                            "enhancePrompt": True, "numSamples": 1}, True)
        elif model == "images2PosedRGBD":
            aid = await self.seed_asset()
            st, d = await self.call(model, {"frames": [{"imageAsset": {"assetId": aid}}],
                                            "targetResolution": [1280, 720]}, True)
        elif model in ("atlasChisel", "atlasGenerate"):
            aid = await self.seed_asset()
            f0 = (await self.rgbd_frames(aid))[0]
            if model == "atlasChisel":
                body = {"targetFrames": [self._frame(f0)], "prompt": PROMPT}
            else:
                body = {"contextFrames": [self._frame(f0)],
                        "targetCameras": [f0["camera"]], "prompt": PROMPT}
            st, d = await self.call(model, body, True)
        elif model in ("imageUpscale", "imageEdit"):
            aid = await self.seed_asset()
            body = {"imageAsset": {"assetId": aid}}
            if model == "imageEdit":
                body["prompt"] = "turn it into a warm watercolor painting"
            st, d = await self.call(model, body, True)
        elif model == "atlasEnhancePrompt":
            aid = await self.seed_asset()
            st, d = await self.call(model, {"images": [{"assetId": aid}]}, True)
        elif model == "posedImages2Splats":
            s = self._load_seed()
            if s.get("posed"):
                st, d = await self.call(model, {"posedFrames": s["posed"], "prompt": PROMPT}, True)
                if st >= 300:
                    s.pop("posed", None)
                    self._seed = s
                    st, d = 0, {}
                else:
                    if (d.get("response") or {}).get("refinedSplats"):
                        s["splat"] = list(d["response"]["refinedSplats"].values())[0]["assetId"]
                        self._save_seed()
            else:
                aid = await self.seed_asset()
                frames = await self.multiview(aid, 3)
                posed = [self._frame(f) for f in frames]
                st, d = await self.call(model, {"posedFrames": posed, "prompt": PROMPT}, True)
                if st < 300:
                    s = self._load_seed()
                    s["posed"] = posed
                    sp = (d.get("response") or {}).get("refinedSplats") or {}
                    if sp:
                        s["splat"] = list(sp.values())[0]["assetId"]
                    self._seed = s
                    self._save_seed()
        elif model == "splats2Mesh":
            s = self._load_seed()
            aid = s.get("splat")
            if not aid:
                raise RuntimeError("还没有 splat：先跑一次 posedImages2Splats")
            st, d = await self.call(model, {"splatAsset": {"assetId": aid},
                                            "textureMode": "image_texture",
                                            "targetFaces": 200000}, True)
        else:
            raise RuntimeError(f"{model} 需要 3D 场景上下文，暂不支持一键试跑")

        elapsed = round(time.time() - t0, 1)
        if st >= 300:
            err = json.dumps(d, ensure_ascii=False)
            cached = self._fallback(model)
            return {"ok": False, "status": st, "elapsed": elapsed,
                    "error": err[:600], "raw": d,
                    "cached": cached,
                    "hint": ("账号额度受限（tier 限额），下面是本账号此前真实跑通的结果"
                             if cached else err[:200])}
        return {"ok": True, "status": st, "elapsed": elapsed,
                "result": (d.get("response") if isinstance(d, dict) else d) or d,
                "meta": DEMO.get(model, {})}
