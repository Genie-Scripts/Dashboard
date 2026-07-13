"""qr.py — QRコードSVG生成（segno 未導入なら None を返し呼び出し側は非表示縮退）"""
from typing import Optional
import logging
logger = logging.getLogger(__name__)

def qr_svg_inline(url: str, size_mm: int = 15) -> Optional[str]:
    try:
        import segno
    except ImportError:
        logger.info("segno 未導入のため QR は省略（pip install segno）")
        return None
    try:
        q = segno.make(url, error="l")   # error=l: 印刷は高コントラスト・URL長82字前後でversionを抑えモジュールを大きく
        import io
        buf = io.BytesIO()
        q.save(buf, kind="svg", xmldecl=False, svgns=True, border=1,
               dark="#1a2332", unit="mm",
               # scale = size_mm / (モジュール数 + border*2)
               scale=size_mm / (q.symbol_size(border=1)[0]))
        return buf.getvalue().decode("utf-8")
    except Exception as e:
        logger.warning(f"QR生成失敗（省略）: {e}")
        return None
