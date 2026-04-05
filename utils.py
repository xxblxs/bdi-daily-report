"""
utils.py — 航运日报通用工具函数
================================
PNG→PDF 转换 + 企微文件推送
所有报告脚本共用，放在仓库根目录

使用方式（接收内存中的 PNG bytes）：
    from utils import convert_and_push_pdf

    convert_and_push_pdf(
        png_bytes=img_bytes,
        webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
        report_title="干散货市场日报",
        date_str="2026-04-03",
    )
"""

import io
import logging
import tempfile
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("report_utils")

# ── img2pdf 可选依赖 ────────────────────────────────────────────────────────

try:
    import img2pdf
    HAS_IMG2PDF = True
except ImportError:
    HAS_IMG2PDF = False
    log.warning("img2pdf 未安装，跳过 PDF 生成。运行: pip install img2pdf")

# ── Pillow 可选（处理 RGBA 透明通道）────────────────────────────────────────

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ════════════════════════════════════════════════════════════════════════════
# PNG bytes → PDF 转换
# ════════════════════════════════════════════════════════════════════════════

def png_bytes_to_pdf(png_bytes: bytes, pdf_path: str) -> str | None:
    """将内存中的 PNG bytes 转为 PDF 文件，返回 PDF 路径。

    自动处理 RGBA 透明通道（img2pdf 不支持 RGBA，需先转 RGB）。
    """
    if not HAS_IMG2PDF:
        return None

    Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)

    # 处理 RGBA 透明通道
    convert_bytes = png_bytes
    if HAS_PILLOW:
        try:
            img = Image.open(io.BytesIO(png_bytes))
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                buf = io.BytesIO()
                bg.save(buf, format="PNG")
                convert_bytes = buf.getvalue()
                log.info("RGBA → RGB 转换完成")
        except Exception as e:
            log.warning(f"Pillow 预处理失败（继续尝试直接转换）: {e}")

    try:
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(convert_bytes))
        size_kb = Path(pdf_path).stat().st_size / 1024
        log.info(f"PDF 生成: {Path(pdf_path).name} ({size_kb:.0f} KB)")
        return pdf_path
    except Exception as e:
        log.error(f"img2pdf 转换失败: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 企微 Webhook 文件推送
# ════════════════════════════════════════════════════════════════════════════

def _extract_webhook_key(webhook_url: str) -> str | None:
    """从企微 webhook URL 中提取 key。"""
    try:
        parsed = urlparse(webhook_url)
        keys = parse_qs(parsed.query).get("key", [])
        return keys[0] if keys else None
    except Exception as e:
        log.error(f"无法解析 webhook key: {e}")
        return None


def push_pdf_to_wecom(pdf_path: str, webhook_url: str) -> bool:
    """通过企微 webhook 推送 PDF 文件。

    流程：
    1. POST upload_media 上传文件 → 获取 media_id
    2. POST send 发送 file 类型消息
    """
    pdf = Path(pdf_path)
    if not pdf.exists():
        log.error(f"PDF 文件不存在: {pdf}")
        return False

    key = _extract_webhook_key(webhook_url)
    if not key:
        log.error("webhook key 解析失败，跳过 PDF 推送")
        return False

    # Step 1: 上传文件
    upload_url = (
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
        f"?key={key}&type=file"
    )
    try:
        with open(pdf, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"media": (pdf.name, f, "application/pdf")},
                timeout=30,
            )
        resp.raise_for_status()
        upload_data = resp.json()

        if upload_data.get("errcode", 0) != 0:
            log.error(f"文件上传失败: {upload_data}")
            return False

        media_id = upload_data["media_id"]
        log.info(f"文件已上传: {pdf.name} -> media_id={media_id[:16]}...")

    except Exception as e:
        log.error(f"文件上传异常: {e}")
        return False

    # Step 2: 发送文件消息
    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    try:
        resp = requests.post(
            send_url,
            json={"msgtype": "file", "file": {"media_id": media_id}},
            timeout=15,
        )
        resp.raise_for_status()
        send_data = resp.json()

        if send_data.get("errcode", 0) != 0:
            log.error(f"文件消息发送失败: {send_data}")
            return False

        log.info(f"PDF 已推送到企微群: {pdf.name}")
        return True

    except Exception as e:
        log.error(f"文件消息发送异常: {e}")
        return False


# ════════════════════════════════════════════════════════════════════════════
# 一站式接口
# ════════════════════════════════════════════════════════════════════════════

def convert_and_push_pdf(
    png_bytes: bytes,
    webhook_url: str,
    report_title: str = "",
    date_str: str = "",
) -> bool:
    """一站式：PNG bytes → PDF → 推送企微。

    Args:
        png_bytes:     Playwright 截图的 PNG 字节
        webhook_url:   企微 webhook 完整 URL
        report_title:  报告标题（用于 PDF 文件名，如 "干散货市场日报"）
        date_str:      日期字符串（如 "2026-04-03"）

    Returns:
        True 全流程成功，False 任一步骤失败
    """
    if not HAS_IMG2PDF:
        log.warning("img2pdf 未安装，跳过 PDF 推送")
        return False

    if not webhook_url:
        log.warning("webhook_url 为空，跳过 PDF 推送")
        return False

    # 生成有意义的文件名
    if report_title and date_str:
        pdf_name = f"{report_title}_{date_str}.pdf"
    elif report_title:
        pdf_name = f"{report_title}.pdf"
    else:
        pdf_name = f"report_{date_str or 'unknown'}.pdf"

    # 使用临时目录存放 PDF
    tmp_dir = tempfile.mkdtemp(prefix="report_pdf_")
    pdf_path = str(Path(tmp_dir) / pdf_name)

    try:
        # 转换
        result = png_bytes_to_pdf(png_bytes, pdf_path)
        if not result:
            return False

        # 推送
        return push_pdf_to_wecom(pdf_path, webhook_url)
    except Exception as e:
        log.error(f"PDF 转换推送异常: {e}")
        return False
