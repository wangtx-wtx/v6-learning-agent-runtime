"""旧版 Office 二进制格式转换（.ppt → .pptx）。

python-pptx / python-docx 只认 OOXML（.pptx / .docx，本质是 ZIP 容器）。
PowerPoint 97-2003 的 .ppt 是 OLE2 复合文档（文件头 ``D0 CF 11 E0``），
``Presentation()`` 会直接抛 ``PackageNotFoundError``。

而 ``file_types.PARSEABLE_KINDS`` 一直宣称支持 ``ppt``，于是这类材料表现为
「上传成功、解析永久 failed」，且 ``parse_error`` 只有一句
``Package not found at '...'`` —— 从这句话看不出是**格式不被支持**，
只会误导人去查文件是否存在（文件明明在，16MB 好好躺在 uploads 里）。

本模块用 PowerPoint COM 完成转换。选择 COM 而不是 LibreOffice：本机已装
Office 时它零额外安装，而 LibreOffice 需要单独分发（~350MB）。两者都不可用时
给出**可操作**的报错（告诉用户去装什么 / 另存为什么），不静默失败。

已知限制
========

* 仅 Windows + 已装 PowerPoint。无此环境时抛 ``OfficeConvertError``，
  材料落到明确失败态，不会伪装 ready。
* COM 调用是同步的，无法从 Python 侧中断。旧版课件转换实测在秒级，但
  若 PowerPoint 进程本身卡死，会占用该 parse worker 直到租约超时
  （``material_worker.PARSE_LEASE_SECONDS``）——这是选择进程内 COM 的代价。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: OLE2 / 复合文档文件头（PowerPoint 97-2003、Word 97-2003 共用）。
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: PowerPoint SaveAs 格式常量：ppSaveAsOpenXMLPresentation（.pptx）。
PP_SAVE_AS_OOXML = 24

#: 探测用户是否已有 PowerPoint 在跑时，tasklist 的最长等待。
_PROBE_TIMEOUT_SECONDS = 10


class OfficeConvertError(RuntimeError):
    """旧版格式转换失败（依赖缺失 / COM 异常 / 输出未生成）。"""


def is_ole2(path: str | Path) -> bool:
    """文件是否为 OLE2 复合文档（旧版二进制 Office 格式）。"""
    try:
        with open(path, "rb") as f:
            return f.read(8) == OLE2_MAGIC
    except OSError:
        return False


def _powerpoint_already_running() -> bool:
    """检测用户是否已经开着自己的 PowerPoint。

    Office 是单实例模型：``DispatchEx`` 想新建的实例在多数情况下会与用户
    已打开的窗口合并。若用户本来就开着 PowerPoint，我们结束时**不能**
    ``Quit()`` —— 那会直接关掉用户正在编辑的演示文稿。

    探测失败时返回 ``True``（宁可少 Quit 一次，也不误关用户窗口）。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/NH"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
        return "POWERPNT.EXE" in (out.stdout or "").upper()
    except Exception:  # noqa: BLE001 - 探测失败按「有」处理
        return True


def _open_presentation(app, src: Path):
    """打开演示文稿；``WithWindow=False`` 被拒时退回带窗口打开。

    PowerPoint COM 是 Office 套件里最不「headless 友好」的一个：部分版本会
    拒绝 ``WithWindow=False``。退回带窗口打开再隐藏，仍优于让整个材料解析失败。
    """
    try:
        return app.Presentations.Open(
            str(src), ReadOnly=True, Untitled=False, WithWindow=False)
    except Exception:  # noqa: BLE001
        logger.debug("WithWindow=False 不被接受，退回带窗口打开", exc_info=True)
        pres = app.Presentations.Open(
            str(src), ReadOnly=True, Untitled=False, WithWindow=True)
        try:
            pres.Windows(1).Visible = False
        except Exception:  # noqa: BLE001 - 隐藏失败不影响转换结果
            pass
        return pres


def _convert_with_com(src: Path, dst: Path) -> None:
    """用 PowerPoint COM 把 ``src`` 另存为 ``dst``。

    必须在**工作线程**内调用（``material_parser`` 经 ``asyncio.to_thread``
    进来），且必须先 ``CoInitialize`` —— 否则 DispatchEx 会抛
    ``com_error(-2147221008, 'CoInitialize has not been called')``。
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        raise OfficeConvertError(
            "未安装 pywin32，无法调用 PowerPoint 转换旧版 .ppt。"
            "请执行: python -m pip install pywin32"
        ) from e

    preexisting = _powerpoint_already_running()
    pythoncom.CoInitialize()
    app = None
    pres = None
    try:
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
        except Exception as e:  # noqa: BLE001
            raise OfficeConvertError(
                f"无法启动 PowerPoint COM（本机是否安装了 PowerPoint？）: "
                f"{type(e).__name__}: {e}"
            ) from e
        try:
            pres = _open_presentation(app, src)
            pres.SaveAs(str(dst), PP_SAVE_AS_OOXML)
        except Exception as e:  # noqa: BLE001
            raise OfficeConvertError(
                f"PowerPoint 转换失败: {type(e).__name__}: {e}") from e
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:  # noqa: BLE001 - 清理失败不得掩盖真实错误
            pass
        try:
            if app is not None and not preexisting:
                app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


def converted_path_for(src: Path, converted_dir: Path) -> Path:
    """转换产物路径（落在受控 converted 目录内，按源文件名派生）。"""
    return Path(converted_dir) / f"{(src.stem or 'material')}.pptx"


def convert_legacy_ppt(src: str | Path, converted_dir: str | Path) -> Path:
    """把旧版二进制 .ppt 转成 .pptx，返回转换后路径。

    幂等：产物已存在且不早于源文件时直接复用（重解析不重复调用 COM）。

    非 OLE2 输入直接报错、不尝试转换 —— 那说明文件是真的损坏（或本来就是
    .pptx 却被误判），此时任何转换都只会产出混乱的错误信息。
    """
    src = Path(src)
    converted_dir = Path(converted_dir)
    if not src.is_file():
        raise OfficeConvertError(f"待转换文件不存在: {src}")
    if not is_ole2(src):
        raise OfficeConvertError(
            f"{src.name} 的文件头不是旧版 OLE2 二进制格式，无法用 PowerPoint 转换。"
            "若该文件已损坏，请在 PowerPoint 中重新导出后再上传"
        )

    dst = converted_path_for(src, converted_dir)
    try:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            logger.info("复用已转换的 pptx: %s", dst.name)
            return dst
    except OSError:
        pass

    converted_dir.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换：转换中途失败不会留下半个 .pptx 被下次误判为
    # 「已转换」，也不会让并发读取看到不完整产物。
    tmp = dst.parent / (dst.name + ".part")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    _convert_with_com(src, tmp)
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise OfficeConvertError(
            f"PowerPoint 未产出有效文件（{tmp.name}），转换未成功")
    tmp.replace(dst)
    logger.info("旧版 .ppt 已转换为 .pptx: %s -> %s", src.name, dst.name)
    return dst
