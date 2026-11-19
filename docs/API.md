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

## 5. posedImages2Splats【3D】

```json
{
  "posedFrames": [
    {"imageAsset": {"assetId": "asset_A"}, "depth": {"depthAsset": {"assetId": "asset_A_d"}},
     "camera": {"extrinsics": {…}, "intrinsics": {…}}, "maskAsset": {…}},
    {"imageAsset": {"assetId": "asset_B"}, "depth": {"depthAsset": {"assetId": "asset_B_d"}},
     "camera": {"extrinsics": {…}, "intrinsics": {…}}, "maskAsset": {…}}
  ]
}
```

- **至少 2 帧**，否则 `422 List should have at least 2 items`
- 帧里**不能带 `centroidWorld`**（`images2PosedRGBD` 的输出带这个字段），否则
  `422 Extra inputs are not permitted`
- 返回 `RUNNING` → 轮询 `operations/{id}`，约 2–3 分钟后 `SUCCEEDED`

```json
{"done": true, "response": {
  "draftSplat": null,
  "refinedSplats": {
    "2000000": {"assetId": "asset_…", "url": "https://storage.googleapis.com/…/asset_….spz?X-Goog-Signature=…"}
  }}}
```

`refinedSplats` 的键是点数档位（`2000000` = 200 万点），值是 `.spz`（Gaussian Splat）资源。

## 6. splats2Mesh【3D】

```json
{"splatAsset": {"assetId": "asset_2ab91c69…"}}
```

返回（也是长任务，约 1 分钟）：

```json
{"done": true, "response": {
  "anchorPositions": [[2.699, 1.688, -0.299], …],
  "colliderBoxes": null,
  "mesh": {"assetId": "asset_f00e2f9e…", "url": "https://storage.googleapis.com/…/asset_….glb?…"}}}
```

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

## 3D 链路示例

```python
# 1) 生成两张图
assets = []
for i in range(2):
    d = httpx.post(f"{BASE}/tasks:atlasTextToImage?wait=true", headers=H,
                   json={"prompt": f"a sunlit stone courtyard, angle {i+1}", "aspectRatio": "16:9"}).json()
    assets.append(d["response"]["frames"][0]["imageAsset"]["assetId"])

# 2) 估位姿（至少 2 帧）
pose = httpx.post(f"{BASE}/tasks:images2PosedRGBD?wait=true", headers=H, json={
    "frames": [{"imageAsset": {"assetId": a}} for a in assets],
    "targetResolution": [1280, 720]}).json()
frames = [{k: v for k, v in f.items() if k != "centroidWorld"}      # 关键：剔除 centroidWorld
          for f in pose["response"]["frames"]]

# 3) 多视图 → splats（长任务，轮询）
sub = httpx.post(f"{BASE}/tasks:posedImages2Splats?wait=true", headers=H,
                 json={"posedFrames": frames}).json()
while not sub.get("done"):
    time.sleep(5)
    sub = httpx.get(f"{BASE}/operations/{sub['id']}?wait=true", headers=H).json()
splat = list(sub["response"]["refinedSplats"].values())[0]["assetId"]

# 4) splats → mesh
sub = httpx.post(f"{BASE}/tasks:splats2Mesh?wait=true", headers=H,
                 json={"splatAsset": {"assetId": splat}}).json()
while not sub.get("done"):
    time.sleep(5)
    sub = httpx.get(f"{BASE}/operations/{sub['id']}?wait=true", headers=H).json()
print(sub["response"]["mesh"]["url"])
```

## 错误码速查

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | `missing Authorization header` | 用 `WLT-Api-Key`，不是 Clerk JWT |
| 422 `Field required` | 缺必填参数 | 看 `detail[].loc` |
| 422 `confidenceAsset` | atlasChisel 深度字段冗余 | 只留 `depthAsset` |
| 422 `centroidWorld` | posedFrames 不接受该字段 | 从 images2PosedRGBD 输出里剔除 |
| 422 `too_short` | posedFrames < 2 | 至少两帧 |
| `done:false / RUNNING` | 长任务 | 轮询 operations |
| 429 | 触发限流 | 退避重试 |
| 404 `accounts/accounts/…` | account 名带前缀 | 去掉 `accounts/` |

---

# 资产上传（用自己的照片做 3D）

三步 + 一个坑，实测跑通（PNG 488KB → `READY`，2720×1536）。

## 流程

```python
H = {"WLT-Api-Key": WLT, "Content-Type": "application/json"}
size = str(len(data)); mime = "image/png"

# 1) 建资产（type=image 必须带 rgb part）
a = httpx.post(f"{B}/assets", headers=H, json={"asset": {
        "displayName": "my.png", "type": "image",
        "parts": [{"role": "rgb", "contentType": mime, "contentLength": size}]}}).json()
aid = a["id"]                       # state = UPLOADING

# 2) 按 part 申请上传地址（注意是复数 createUploadUrls）
u = httpx.post(f"{B}/assets/{aid}:createUploadUrls", headers=H, json={
        "parts": [{"role": "rgb", "contentType": mime, "contentLength": size}]}).json()["uploads"][0]

# 3) PUT 上传 —— headers 必须原样用接口返回的
httpx.put(u["url"], content=data, headers=u["headers"])

# 4) 收尾
info = httpx.post(f"{B}/assets/{aid}:completeUpload", headers=H, json={}).json()
# → {"state":"READY","width":2720,"height":1536,"sizeBytes":"6939001", ...}
```

## 踩过的坑

| 现象 | 原因 | 解法 |
|---|---|---|
| `422 Field required: asset` | body 要包一层 `{"asset": {...}}` | 加上 |
| `400 asset type 'image' requires at least one of rgb, depth` | 漏了 part | `parts:[{"role":"rgb",...}]` |
| `422 Field required: contentType/contentLength` | part 必填 | 补 `contentType` + `contentLength`（字符串）|
| `400 has a type, so its bytes are granted per part by :createUploadUrls` | 用错了单数接口 | 用 **`:createUploadUrls`** |
| GCS `MalformedSecurityHeader` | 少发签名头 | 必须发 `x-goog-if-generation-match` 等**全部** headers |
| GCS `SignatureDoesNotMatch` | `x-goog-content-length-range` 写成 `0,L` | 正确格式是 **`L,L`**（min,max 都等于长度） |

**结论：上传的 headers 不要自己拼，直接用 `uploads[i]["headers"]`。**

## 网关封装

```bash
curl -X POST http://127.0.0.1:8790/v1/assets \
  -H "Authorization: Bearer sk-pool-xxxx" \
  -F "file=@photo.png;type=image/png"
# → {"assetId":"asset_...","state":"READY","width":2480,"height":2500,"sizeBytes":"488519"}
```

拿到的 `assetId` 直接喂给 `images2PosedRGBD`：

```json
{"frames":[{"imageAsset":{"assetId":"asset_..."}}], "targetResolution":[1280,720]}
```

## 操作控制

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/operations/{id}/cancel` | 取消正在跑的任务 |
| `GET` | `/v1/operations/{id}/trace` | 查看执行链路 |

## Webhook（免轮询）

任务 body 里加 `webhookUrl`（https、443、不可与 `?wait=true` 同用），
Atlas 会在完成时 POST 一个签名的 `task.succeeded` / `task.failed`，失败重试 24h，
签名用 `/.well-known/webhooks/jwks.json` 公钥验证。

---

# 站内能力（web-api / NOVA 通道）

`GET /api/v2/accounts/{acct}/web-api/capabilities` 返回该账号**站内**可用的全部任务
（需要 Clerk 会话，`WLT-Api-Key` 调用会 401）：

```json
{"tasks":["atlasChisel","atlasEnhancePrompt","atlasGenerate","atlasTextToImage",
          "chiselCaption","chiselCaptionUpdate","imageEdit","imageUpscale",
          "images2PosedRGBD","posedImages2Splats","splats2Mesh"]}
```

比对外开放的 Developer API（6 个）多 5 个。

## 调用方式

```python
# 1) 创建/复用一个 web session
POST /api/v2/accounts/{acct}/projects/{proj}/web-api/sessions      → wsess_xxx
# 2) 发任务
POST /api/v2/accounts/{acct}/projects/{proj}/web-api/sessions/{wsess}/tasks:{task}
# 3) 轮询
GET  /api/v2/operations/{id}?wait=true
```

鉴权用 Clerk JWT（60s 过期），但可以纯 HTTP 刷新，无需常开浏览器：

```python
POST https://clerk.worldlabs.ai/v1/client/sessions/{sid}/tokens
     Cookie: __client, __session, ...
  → {"jwt": "eyJ..."}
```

`atlasreg/clerk_session.py` 已封装：从浏览器 profile 导出 cookie 一次 → 之后自动续期。

## 五个站内任务（实测端点可用）

| task | 必需参数 | 说明 |
|---|---|---|
| `imageUpscale` | `imageAsset.assetId` | 图像放大（201，约 8s） |
| `imageEdit` | `imageAsset` + `prompt` | 图生图编辑（201） |
| `atlasEnhancePrompt` | `images[]`（对象数组，不是 id 字符串） | 提示词增强（文字模型） |
| `chiselCaption` | `entities` `views` `objectIdLegend` `projection` | 场景自动描述 |
| `chiselCaptionUpdate` | `entities` `views` | 更新描述 |

返回的 `metadata.surface` 是 `NOVA`，与 Developer API 的 `DEVELOPER_API` 区分。

网关已把两者统一到 `/v1/tasks/{model}`：模型清单带 `provider` 字段（`atlas-dev` / `atlas-web`），
调用方无需关心走哪条通道。

---

# 站内 LLM 模型（Chisel Agent）

`Chisel` 界面里有一个内置 agent，**按 token 消耗积分**（页面原文：*"Chisel has a built-in agent to assist you (LLM usage requires credits)"*）。
它的模型选择器里有 9 个模型，前端常量（`placement-budget-*.js`）里的真实值和定价：

| 显示名 | 真实 model 名 | 输入（积分/1M token） | 输出 |
|---|---|---|---|
| GPT-6 Luna | `gpt-6-luna` | 100 | 500 |
| GLM 5.3 Flash | `zai/glm-5.3-flash` | 150 | 500 |
| Gemini 3.8 Flash | `google/gemini-3.8-flash` | 750 | 3750 |
| GLM 5.3 | `zai/glm-5.3` | 1200 | 4000 |
| GPT-6.1 Terra | `gpt-6.1-terra` | 1000 | 5000 |
| Claude Sonnet 5.5 | `anthropic/claude-sonnet-5.5` | 2000 | 10000 |
| GPT-6.1 Sol | `gpt-6.1-sol` | 2000 | 10000 |
| Gemini 4 Argon | `gemini-4-argon` | 2000 | 10000 |
| Gemini 3.1 Pro | `gemini-3.1-pro` | 2000 | 12000 |
| GPT-6 Astra | `gpt-6-astra` / `gpt-6-astra-fast` | — | — |
| Claude Opus 5.5 | `claude-opus-5.5` | — | — |

## 能怎么调

| 通道 | 能否指定这些 model | 说明 |
|---|---|---|
| Developer API（`WLT-Api-Key`） | ❌ | 6 个 task 里没有 LLM，`modelParameters` 只有 seed/numSteps |
| 站内 web-api（JWT） | ❌ | 11 个 task 的 body 加 `model` 会被 `extra_forbidden` 拒掉 |
| **Chisel Agent（UI）** | ✅ | 唯一入口，agent 循环内部调用，产出是 3D 场景 |

Agent 的协议字段在前端可见：`agentAsk` / `agentPlan` / `agentWait` / `agentPlanRender`，
事件里带 `toolCall` / `toolResults` / `turns` / `tokens`，属于**流式 agent 会话**，不是简单的 chat completion。

## 顺带拿到的两个实用接口

```http
GET  /api/v2/accounts/{acct}/web-api/billing/subscription/creditBalance?includeGrants=true
  → {"totalCredits":9845,"creditGrants":[]}

POST /api/v2/betaAgreement:accept
  {"version":"atlas-marble2-beta-2026-10-29","accepted":true}
  → 接受条款，否则 /workflows/* 页面永远卡在 "Accept the terms"
```

网关已封装成 `GET /v1/credits`（实时读真实余额）。
