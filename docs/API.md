# Atlas / Marble 模型调用规范（实测）

> 全部内容来自对 `atlas.worldlabs.ai` 前端的逆向 + 真实调用验证。
> 每次调用的原始响应存在 `docs/r_*.json`。

## 端点与鉴权

```
POST https://atlas.worldlabs.ai/api/v2/tasks:{task}?wait=true
GET  https://atlas.worldlabs.ai/api/v2/operations/{id}?wait=true

Header:
  WLT-Api-Key: wl_live_xxxxxxxx          # 必填
  Content-Type: application/json
  Idempotency-Key: <uuid>                # 建议
```

- `?wait=true` 会阻塞到任务完成（文生图约 30s），不加则立即返回 operation id，用 `/operations/{id}` 轮询。
- API Key 在 Atlas 后台「API Keys」页生成，或由账号 JWT 签发：

```
POST /api/v2/accounts/{acct}/projects/{proj}/apiKeys
{"apiKey":{"displayName":"x","scopes":["tasks.create","operations.read","operations.cancel","assets.read","assets.create"]}}
→ {"apiKey":{...},"secret":"wl_live_xxxx"}
```

## 四个任务

| task | 用途 | 输入要点 | 实测耗时 |
|---|---|---|---|
| `atlasTextToImage` | 文生图 | `prompt` | 32.6s |
| `images2PosedRGBD` | 图 → 相机位姿 + 线性深度 | `frames[].imageAsset.assetId` | 6.1s |
| `atlasGenerate` | 位姿/深度上下文 → 生成新视角 | `contextFrames[]` + `targetCameras[]` | 19.0s |
| `atlasChisel` | 深度目标帧 → 生成视角 | `targetFrames[]`（含深度） | 10.7s |

所有任务返回结构一致：

```json
{
  "id": "…", "done": true, "error": null,
  "metadata": {"task":"atlasTextToImage","state":"SUCCEEDED","surface":"DEVELOPER_API"},
  "response": {"frames":[{"imageAsset":{"assetId":"asset_…","url":"https://storage.googleapis.com/…"},
                          "maskAsset":{…}, "camera":{…}, "depth":{…}}]}
}
```

`imageAsset.url` 是带签名的 GCS 直链，可直接下载/展示。

---

## 1. atlasTextToImage

```json
{
  "prompt": "a small greenhouse at dawn, warm light, wide view",
  "aspectRatio": "16:9",
  "enhancePrompt": true,
  "numSamples": 1
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| prompt | string | ✅ | 场景描述 |
| aspectRatio | string | — | `16:9` / `1:1` / `9:16` 等 |
| enhancePrompt | bool | — | 让服务端自动扩写提示词 |
| numSamples | int | — | 生成张数 |

返回 `frames[]`，每帧含 `imageAsset` / `maskAsset` / `camera` / `depth`。

---

## 2. images2PosedRGBD

```json
{
  "frames": [{"imageAsset": {"assetId": "asset_4ebbbbcec83d20310e199235360ec5e7"}}],
  "targetResolution": [1280, 720],
  "canonicalizeToFirstFrame": false
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| frames[].imageAsset.assetId | ✅ | 要估计位姿的图片资源（可先用 atlasTextToImage 产出，或走 assets 上传） |
| targetResolution | — | `[宽,高]`，后续任务要求 1280×720 |
| canonicalizeToFirstFrame | — | 把首帧作为世界原点 |

返回的每帧比其它任务多一个 `centroidWorld`，`camera` 是真实估计出的内外参：

```json
"camera": {
  "extrinsics": {"position":[0.0,2.139,0.0],"quaternion":[-0.0138,-0.0001,-0.0088,0.9999],"coordinateSystem":"rub"},
  "intrinsics": {"fx":672.67,"fy":673.14,"cx":640.0,"cy":360.5,"width":1280,"height":720}
}
```

---

## 3. atlasGenerate

```json
{
  "contextFrames": [{
    "imageAsset": {"assetId": "asset_4ebbbbcec83d20310e199235360ec5e7"},
    "depth": {"depthAsset": {"assetId": "asset_…"}},
    "camera": { "extrinsics": {…}, "intrinsics": {…} }
  }],
  "targetCameras": [{
    "extrinsics": {"position":[0,1.6,0],"quaternion":[0,0,0,1],"coordinateSystem":"rub"},
    "intrinsics": {"width":1280,"height":720,"fx":900,"fy":900,"cx":640,"cy":360}
  }],
  "prompt": "a sunlit stone courtyard with climbing jasmine",
  "enhancePrompt": true,
  "returnDepth": false
}
```

- `contextFrames`：已知视角（图 + 深度 + 相机）
- `targetCameras`：要生成的新视角（只需相机）
- 最省事的用法：拿 `images2PosedRGBD` 的输出帧直接当 `contextFrames`，`targetCameras` 填一个新机位。

---

## 4. atlasChisel

```json
{
  "targetFrames": [{
    "camera": { "extrinsics": {…}, "intrinsics": {…} },
    "depth": {"depthAsset": {"assetId": "asset_…"}}
  }],
  "prompt": "a pale stone room with soft morning light",
  "enhancePrompt": true,
  "returnDepth": false
}
```

**坑**：`images2PosedRGBD` 输出的 `depth` 同时带 `depthAsset` 和 `confidenceAsset`，
直接透传会 422：

```
Value error, targetFrames[0].depth carries a confidenceAsset;
atlasChisel does not read a depth confidence
```

只保留 `depthAsset` 即可：

```python
depth = {"depthAsset": pose_frame["depth"]["depthAsset"]}
```

---

## 完整链路示例

```python
import httpx, json

BASE = "https://atlas.worldlabs.ai/api/v2"
H = {"WLT-Api-Key": "wl_live_xxxx", "Content-Type": "application/json"}

# 1) 文生图
t2i = httpx.post(f"{BASE}/tasks:atlasTextToImage?wait=true", headers=H,
                 json={"prompt": "a small greenhouse at dawn"}).json()
asset = t2i["response"]["frames"][0]["imageAsset"]["assetId"]

# 2) 估位姿 + 深度
pose = httpx.post(f"{BASE}/tasks:images2PosedRGBD?wait=true", headers=H,
                  json={"frames": [{"imageAsset": {"assetId": asset}}],
                        "targetResolution": [1280, 720]}).json()
frame = pose["response"]["frames"][0]

# 3) 换个机位生成新视角
gen = httpx.post(f"{BASE}/tasks:atlasGenerate?wait=true", headers=H, json={
    "contextFrames": [{"imageAsset": {"assetId": asset},
                       "depth": {"depthAsset": frame["depth"]["depthAsset"]},
                       "camera": frame["camera"]}],
    "targetCameras": [{"extrinsics": {"position": [0.8, 1.6, 0.4],
                                      "quaternion": [0, 0, 0, 1],
                                      "coordinateSystem": "rub"},
                       "intrinsics": {"width": 1280, "height": 720,
                                      "fx": 900, "fy": 900, "cx": 640, "cy": 360}}],
    "prompt": "same courtyard, morning light", "returnDepth": False}).json()

print(gen["response"]["frames"][0]["imageAsset"]["url"])
```

## 错误码速查

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | `missing Authorization header` | 用 `WLT-Api-Key`，不是 Clerk JWT |
| 422 `Field required` | 缺必填参数 | 看 `detail[].loc` |
| 422 `confidenceAsset` | atlasChisel 深度字段冗余 | 只留 `depthAsset` |
| 429 | 触发限流 | 退避重试 |
| 404 `accounts/accounts/…` | account 名带前缀 | 去掉 `accounts/` |
