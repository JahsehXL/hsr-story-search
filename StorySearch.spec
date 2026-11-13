from pathlib import Path
import sqlite_vec

root = Path(SPECPATH)
vec_path = Path(sqlite_vec.__file__).resolve().parent
vector_binaries = [(str(path), "sqlite_vec") for path in vec_path.iterdir() if path.suffix in (".dll", ".so", ".dylib")]

analysis = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=vector_binaries,
    datas=[(str(root / "frontend" / "dist"), "frontend/dist"), (str(root / "backend" / "resources"), "backend/resources")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "sqlite_vec"],
    excludes=["tkinter"],
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="StorySearch", console=True)
collection = COLLECT(executable, analysis.binaries, analysis.datas, name="StorySearch")
