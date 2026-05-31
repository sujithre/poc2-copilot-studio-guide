"""Per-page vision extraction with caching and Pydantic Structured Outputs.

For each rasterized page image:
  1. Compute cache key = sha256(image_bytes) + prompt_version + model deployment.
  2. If JSON exists with same cache key, reuse.
  3. Otherwise call Azure OpenAI / Foundry vision deployment using `.parse()` with
     PageExtraction (Pydantic) as the structured-output schema.
  4. Persist JSON.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import AzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from auth import aoai_token_provider
from common import env, load_manifest, docs_to_process, paths
from schema import PageExtraction, SYSTEM_PROMPT


def make_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT", required=True),
        azure_ad_token_provider=aoai_token_provider(),
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


def cache_key(image_bytes: bytes, prompt_version: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(image_bytes)
    h.update(b"\x00" + prompt_version.encode())
    h.update(b"\x00" + model.encode())
    return h.hexdigest()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
def call_vision(client: AzureOpenAI, deployment: str, image_b64: str, doc_id: str, page_no: int) -> PageExtraction:
    user_text = (
        f"doc_id: {doc_id}\n"
        f"page: {page_no}\n"
        "Extract the page now. Return JSON only that matches the structured-output schema."
    )
    # Reasoning models (gpt-5, o-series) require `max_completion_tokens` and
    # do not accept custom `temperature`. Older models (gpt-4o, gpt-4.1) use
    # `max_tokens` and accept `temperature=0`. Detect from deployment name.
    is_reasoning = any(
        deployment.lower().startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
    ) or "gpt-5" in deployment.lower()
    kwargs: dict = {
        "model": deployment,
        "timeout": 180,
        "response_format": PageExtraction,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"}},
                ],
            },
        ],
    }
    if is_reasoning:
        kwargs["max_completion_tokens"] = 16000
        # temperature is fixed at 1 for reasoning models - omit it
    else:
        kwargs["max_tokens"] = 8000
        kwargs["temperature"] = 0
    completion = client.beta.chat.completions.parse(**kwargs)
    msg = completion.choices[0].message
    if getattr(msg, "refusal", None):
        raise RuntimeError(f"Vision model refused: {msg.refusal}")
    parsed = msg.parsed
    if parsed is None:
        raise RuntimeError("Vision model returned no parsed object")
    return parsed


def extract_one_page(client: AzureOpenAI, deployment: str, prompt_version: str,
                     doc_id: str, image_path: Path, json_path: Path) -> dict:
    image_bytes = image_path.read_bytes()
    key = cache_key(image_bytes, prompt_version, deployment)
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            if existing.get("_cache_key") == key and existing.get("_status") == "ok":
                return {"page": existing.get("page"), "status": "cached"}
        except Exception:
            pass

    page_no = int(image_path.stem.replace("page", ""))
    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        parsed = call_vision(client, deployment, image_b64, doc_id, page_no)
        obj = parsed.model_dump()
        obj["doc_id"] = doc_id
        obj["page"] = page_no
        status = "ok"
    except Exception as e:
        # Unwrap tenacity RetryError to surface the real underlying API error
        # (e.g. BadRequestError body with the specific reason).
        underlying = getattr(e, "last_attempt", None)
        if underlying is not None:
            try:
                root = underlying.exception()
                if root is not None:
                    e = root
            except Exception:
                pass
        body = getattr(getattr(e, "response", None), "text", "")
        err_msg = f"{type(e).__name__}: {e}"
        if body and body not in err_msg:
            err_msg = f"{err_msg} | body={body[:500]}"
        obj = {"doc_id": doc_id, "page": page_no, "error": err_msg}
        status = f"error: {err_msg}"

    obj["_cache_key"] = key
    obj["_status"] = status
    json_path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"page": page_no, "status": status}


def extract_doc(client: AzureOpenAI, deployment: str, prompt_version: str,
                doc_id: str, pages_dir: Path, vision_dir: Path,
                concurrency: int) -> list[dict]:
    vision_dir.mkdir(parents=True, exist_ok=True)
    images = sorted(pages_dir.glob("page*.png"))
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {
            ex.submit(extract_one_page, client, deployment, prompt_version, doc_id, img,
                      vision_dir / (img.stem + ".json")): img
            for img in images
        }
        for fut in tqdm(as_completed(futs), total=len(futs), desc=f"vision {doc_id}", unit="pg"):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"page": futs[fut].name, "status": f"error: {e}"})
    return results


def main() -> None:
    p = paths()
    deployment = env("AZURE_OPENAI_VISION_DEPLOYMENT", required=True)
    prompt_version = env("VISION_PROMPT_VERSION", "v1")
    concurrency = int(env("VISION_MAX_CONCURRENCY", "4"))
    client = make_client()

    manifest = load_manifest()
    summary = []
    for doc in docs_to_process(manifest):
        pages_dir = p["pages"] / doc["doc_id"]
        vision_dir = p["vision"] / doc["doc_id"]
        if not pages_dir.exists():
            print(f"SKIP, no rasterized pages: {pages_dir}")
            continue
        res = extract_doc(client, deployment, prompt_version, doc["doc_id"], pages_dir, vision_dir, concurrency)
        ok = sum(1 for r in res if r.get("status") in ("ok", "cached"))
        summary.append({"doc_id": doc["doc_id"], "pages": len(res), "ok_or_cached": ok})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
