# PyInstaller spec: builds dist\Runemark\Runemark.exe (one folder, no console window).
# Run from the project root:  pyinstaller packaging\runemark.spec --noconfirm
from pathlib import Path

root = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
ffmpeg_dir = root / "vendor" / "ffmpeg"
binaries = [(str(ffmpeg_dir / name), "ffmpeg") for name in ("ffmpeg.exe", "ffprobe.exe")
            if (ffmpeg_dir / name).exists()]
datas = [(str(ffmpeg_dir / "LICENSE.txt"), "ffmpeg")] if (ffmpeg_dir / "LICENSE.txt").exists() else []

a = Analysis(  # noqa: F821
    [str(root / "runemark" / "__main__.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "win32com.propsys.propsys", "win32com.propsys.pscon", "pythoncom", "pywintypes",
        "send2trash.win.modern", "send2trash.win.legacy",
    ],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
              "PySide6.QtPdf", "PySide6.QtDesigner"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Runemark",
    console=False,
    icon=str(root / "packaging" / "runemark.ico"),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Runemark")  # noqa: F821
