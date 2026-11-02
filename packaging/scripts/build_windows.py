"""Build reproduzível do Sysvar Organizer para Windows 11 (64 bits).

Executar pelo atalho ``packaging\\build_windows.cmd``, que usa o Python do
ambiente virtual do projeto (``.venv``). Etapas:

1. valida pré-requisitos (Windows 64 bits, Python 3.11–3.13 em ambiente
   virtual, PySide6, Pillow, NumPy e PyInstaller instalados);
2. prepara as pastas próprias do build (``build\\windows`` e ``dist\\windows``),
   apagando somente pastas criadas por este script (marcador);
3. gera o arquivo de versão do executável e executa o PyInstaller;
4. confere que o executor físico NÃO entrou no pacote;
5. copia os textos de licença dos componentes distribuídos e grava
   ``build-info.json`` (versões e hashes, para rastreabilidade);
6. executa o diagnóstico do executável gerado num diretório de dados isolado,
   fora da pasta do código e sem Python no PATH;
7. gera o instalador com o Inno Setup, quando ele estiver instalado.

Nada é instalado automaticamente e nenhum arquivo fora das pastas do build é
alterado. Códigos de saída: 0 sucesso; 1 erro; 2 executável gerado, mas
instalador não (Inno Setup ausente); 3 pré-requisito ausente.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
SPEC = PACKAGING / "pyinstaller" / "sysvar_organizer.spec"
ISS = PACKAGING / "installer" / "sysvar_organizer.iss"
REQUIREMENTS = PACKAGING / "scripts" / "requirements-build.txt"
WORK_DIR = ROOT / "build" / "windows"
DIST_DIR = ROOT / "dist" / "windows"
APP_NAME = "SysvarOrganizer"
MARKER = ".sysvar-organizer-build"
TEST_ONLY_MODULES = (
    "sysvar_organizer.operations.executor",
    "sysvar_organizer.operations.safety",
)
REQUIRED_MODULES = {"PySide6": "PySide6", "PIL": "Pillow", "numpy": "numpy", "PyInstaller": "pyinstaller"}
# Distribuições cujo código vai dentro do pacote (licenças a acompanhar).
BUNDLED_DISTRIBUTIONS = (
    "PySide6",
    "PySide6_Essentials",
    "shiboken6",
    "pillow",
    "numpy",
    "pyinstaller",
)
LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "authors", "third-party", "thirdparty")
SELF_CHECK_TIMEOUT = 240

EXIT_OK, EXIT_ERROR, EXIT_NO_INSTALLER, EXIT_PREREQUISITE = 0, 1, 2, 3


class BuildError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def say(text: str) -> None:
    print(f"[build] {text}", flush=True)


# Pré-requisitos ---------------------------------------------------------------

def read_version(root: Path = ROOT) -> str:
    """Versão declarada em sysvar_organizer/__init__.py (sem importar o pacote)."""
    tree = ast.parse((root / "sysvar_organizer" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise BuildError("Não foi possível ler __version__ em sysvar_organizer/__init__.py.")


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split(".")]
    if not 1 <= len(parts) <= 4:
        raise BuildError(f"Versão inválida: {version}")
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def check_prerequisites() -> None:
    if sys.platform != "win32":
        raise BuildError("O build do executável Windows deve ser feito no Windows.", EXIT_PREREQUISITE)
    if struct.calcsize("P") != 8 or not platform.machine().lower().endswith("64"):
        raise BuildError("É necessário Windows e Python de 64 bits.", EXIT_PREREQUISITE)
    if not (3, 11) <= sys.version_info[:2] <= (3, 13):
        raise BuildError(
            f"Python {platform.python_version()} não suportado: use 3.11, 3.12 ou 3.13.",
            EXIT_PREREQUISITE,
        )
    if sys.prefix == sys.base_prefix:
        raise BuildError(
            "Execute o build com o Python do ambiente virtual do projeto (.venv), "
            "nunca com o Python global: use packaging\\build_windows.cmd.",
            EXIT_PREREQUISITE,
        )
    missing = [dist for module, dist in REQUIRED_MODULES.items() if importlib.util.find_spec(module) is None]
    if missing:
        raise BuildError(
            "Componentes ausentes no ambiente virtual: "
            + ", ".join(missing)
            + ".\nInstale-os SOMENTE no ambiente do projeto, com:\n"
            f'  "{sys.executable}" -m pip install -e . -r "{REQUIREMENTS}"',
            EXIT_PREREQUISITE,
        )
    for path in (SPEC, ISS, ROOT / "sysvar_organizer" / "launcher.py"):
        if not path.is_file():
            raise BuildError(f"Arquivo do build ausente: {path}", EXIT_PREREQUISITE)


def find_iscc(explicit: str | None = None) -> Path | None:
    """Localiza o compilador do Inno Setup 6 (ISCC.exe), sem instalar nada."""
    candidates: list[Path] = []
    for value in (explicit, os.environ.get("ISCC")):
        if value:
            candidates.append(Path(value))
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    if found:
        candidates.append(Path(found))
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            sub = "Programs" if env == "LOCALAPPDATA" else ""
            candidates.append(Path(base, sub, "Inno Setup 6", "ISCC.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Pastas do build ----------------------------------------------------------------

def owned_directory(path: Path, root: Path = ROOT) -> Path:
    """Recria uma pasta do build. Só apaga pastas que este script criou."""
    path = path.resolve()
    root = root.resolve()
    if os.path.commonpath([str(path), str(root)]) != str(root) or path == root:
        raise BuildError(f"Pasta de build fora do repositório recusada: {path}")
    if path.exists():
        if not (path / MARKER).is_file():
            raise BuildError(
                f"A pasta {path} já existe e não foi criada por este build; ela NÃO será "
                "apagada. Mova ou renomeie a pasta e execute novamente."
            )
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / MARKER).write_text("Pasta gerada pelo build do Sysvar Organizer.\n", encoding="utf-8")
    return path


def write_version_file(target: Path, version: str) -> Path:
    numbers = version_tuple(version)
    strings = {
        "CompanyName": "Sysvar",
        "FileDescription": "Sysvar Organizer - Organizador de arquivos",
        "FileVersion": version,
        "InternalName": APP_NAME,
        "LegalCopyright": "Sysvar",
        "OriginalFilename": f"{APP_NAME}.exe",
        "ProductName": "Sysvar Organizer",
        "ProductVersion": version,
    }
    table = ",\n".join(f"          StringStruct({k!r}, {v!r})" for k, v in strings.items())
    target.write_text(
        f"""# Gerado pelo build do Sysvar Organizer. Não editar.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '041604B0',
        [
{table}
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [1046, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return target


# Verificações do pacote -------------------------------------------------------

def bundled_module_names(work_dir: Path) -> str:
    """Conteúdo do índice de módulos Python do pacote (PYZ-00.toc)."""
    tocs = sorted(work_dir.rglob("PYZ-00.toc"))
    if not tocs:
        raise BuildError("Índice de módulos do PyInstaller (PYZ-00.toc) não encontrado.")
    return tocs[0].read_text(encoding="utf-8", errors="replace")


def check_bundle_contents(toc_text: str) -> None:
    present = [m for m in TEST_ONLY_MODULES if f"'{m}'" in toc_text]
    if present:
        raise BuildError(
            "SEGURANÇA: o executor físico entrou no pacote (" + ", ".join(present) + "). "
            "O pacote foi recusado."
        )
    for required in ("sysvar_organizer.app", "sysvar_organizer.reports.export", "sysvar_organizer.diagnostics"):
        if f"'{required}'" not in toc_text:
            raise BuildError(f"Módulo obrigatório ausente do pacote: {required}")


def collect_licenses(app_dir: Path, distributions=BUNDLED_DISTRIBUTIONS) -> dict:
    """Copia os textos de licença dos componentes distribuídos para ``licencas``."""
    target_root = app_dir / "licencas"
    copied: dict[str, list[str]] = {}
    missing: list[str] = []
    for name in distributions:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        files = []
        for entry in dist.files or []:
            parts = [p.lower() for p in Path(str(entry)).parts]
            in_dist_info = parts and parts[0].endswith((".dist-info", ".egg-info"))
            base = parts[-1] if parts else ""
            license_like = base.startswith(LICENSE_PREFIXES) or "licenses" in parts[:-1]
            if in_dist_info and license_like:
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    rel = Path(*Path(str(entry)).parts[1:])
                    destination = target_root / f"{dist.metadata['Name']}-{dist.version}" / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    files.append(str(rel))
        if files:
            copied[f"{dist.metadata['Name']} {dist.version}"] = sorted(files)
        else:
            missing.append(name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        destination = target_root / f"Python-{platform.python_version()}" / "LICENSE.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, destination)
        copied[f"Python {platform.python_version()}"] = ["LICENSE.txt"]
    else:
        missing.append("Python")
    return {"copiadas": copied, "sem_texto_de_licenca_localizado": missing}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(root: Path = ROOT) -> str | None:
    """Commit atual lido diretamente de .git (sem executar o git)."""
    head = root / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref: "):
            ref = root / ".git" / text[5:]
            if ref.is_file():
                return ref.read_text(encoding="utf-8").strip()
            packed = root / ".git" / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(text[5:]):
                    return line.split()[0]
            return None
        return text
    except OSError:
        return None


def package_versions() -> dict[str, str | None]:
    versions = {}
    for name in ("PySide6", "PySide6_Essentials", "shiboken6", "pillow", "numpy", "pyinstaller",
                 "pyinstaller-hooks-contrib"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def write_build_info(app_dir: Path, version: str, licenses: dict, iscc: Path | None) -> Path:
    exe = app_dir / f"{APP_NAME}.exe"
    files = [p for p in app_dir.rglob("*") if p.is_file()]
    info = {
        "aplicativo": "Sysvar Organizer",
        "versao": version,
        "commit": git_commit(),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "plataforma": platform.platform(),
        "componentes": package_versions(),
        "inno_setup": str(iscc) if iscc else None,
        "executavel_sha256": sha256_of(exe) if exe.is_file() else None,
        "arquivos": len(files),
        "tamanho_bytes": sum(p.stat().st_size for p in files),
        "modulos_excluidos": list(TEST_ONLY_MODULES),
        "licencas": licenses,
    }
    target = app_dir.parent / "build-info.json"
    target.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def isolated_environment(data_dir: Path) -> dict[str, str]:
    """Ambiente sem Python externo: PATH só com pastas do Windows."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP", "PATH")}
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join(
        [str(Path(system_root, "System32")), system_root, str(Path(system_root, "System32", "Wbem"))]
    )
    env["SYSVAR_ORGANIZER_DATA_DIR"] = str(data_dir)
    return env


def run_packaged_self_check(exe: Path, data_dir: Path) -> str:
    """Diagnóstico do executável gerado, com dados isolados e fora do repositório."""
    with tempfile.TemporaryDirectory(prefix="sysvar-organizer-verificacao-") as cwd:
        try:
            result = subprocess.run(
                [str(exe), "--self-check", "--no-dialog"],
                cwd=cwd,
                env=isolated_environment(data_dir),
                timeout=SELF_CHECK_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BuildError("O diagnóstico do executável não terminou no tempo limite.") from exc
    report = data_dir / "logs" / "self-check.txt"
    text = report.read_text(encoding="utf-8") if report.is_file() else ""
    if result.returncode != 0 or not text:
        raise BuildError(
            f"O diagnóstico do executável falhou (código {result.returncode}).\n{text}".strip()
        )
    return text


# Execução -----------------------------------------------------------------------

def build(args: argparse.Namespace) -> int:
    check_prerequisites()
    version = read_version()
    iscc = None if args.skip_installer else find_iscc(args.iscc)
    say(f"Sysvar Organizer {version} — Python {platform.python_version()} ({sys.executable})")

    work = owned_directory(WORK_DIR)
    dist = owned_directory(DIST_DIR)
    version_file = write_version_file(work / "version_info.txt", version)

    say("Gerando o aplicativo com o PyInstaller…")
    env = dict(os.environ, SYSVAR_ORGANIZER_VERSION_FILE=str(version_file))
    command = [
        sys.executable, "-m", "PyInstaller", str(SPEC),
        "--noconfirm",
        "--workpath", str(work / "pyinstaller"),
        "--distpath", str(dist),
    ]
    if subprocess.run(command, cwd=ROOT, env=env, check=False).returncode != 0:
        raise BuildError("O PyInstaller falhou; veja as mensagens acima.")
    app_dir = dist / APP_NAME
    exe = app_dir / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise BuildError(f"Executável não encontrado após o PyInstaller: {exe}")

    check_bundle_contents(bundled_module_names(work / "pyinstaller"))
    say("Conferido: executor físico ausente do pacote.")
    licenses = collect_licenses(app_dir)
    if licenses["sem_texto_de_licenca_localizado"]:
        say("ATENÇÃO: licença não localizada para: " + ", ".join(licenses["sem_texto_de_licenca_localizado"]))

    if args.skip_self_check:
        say("ATENÇÃO: diagnóstico do executável NÃO executado (--skip-self-check).")
    else:
        say("Executando o diagnóstico do executável (dados isolados, sem Python no PATH)…")
        print(run_packaged_self_check(exe, work / "diagnostico-dados"), flush=True)

    info = write_build_info(app_dir, version, licenses, iscc)
    say(f"Aplicativo: {app_dir}")
    say(f"Executável: {exe}")
    say(f"Registro do build: {info}")

    if args.skip_installer:
        say("Instalador não gerado (--skip-installer).")
        return EXIT_OK
    if iscc is None:
        say(
            "Instalador NÃO gerado: Inno Setup 6 não encontrado. Instale o Inno Setup 6 "
            "(https://jrsoftware.org/isdl.php) e execute novamente, ou informe o caminho com "
            "--iscc \"C:\\...\\ISCC.exe\"."
        )
        return EXIT_NO_INSTALLER
    installer_dir = dist / "instalador"
    installer_dir.mkdir()
    say(f"Gerando o instalador com {iscc}…")
    command = [
        str(iscc),
        f"/DAppVersion={version}",
        f"/DSourceDir={app_dir}",
        f"/DOutputDir={installer_dir}",
        str(ISS),
    ]
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        raise BuildError("O Inno Setup falhou; veja as mensagens acima.")
    setup = installer_dir / f"{APP_NAME}-{version}-setup.exe"
    if not setup.is_file():
        raise BuildError(f"Instalador não encontrado: {setup}")
    say(f"Instalador: {setup}")
    say(f"SHA-256 do instalador: {sha256_of(setup)}")
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build do Sysvar Organizer para Windows.")
    parser.add_argument("--iscc", help="Caminho do ISCC.exe do Inno Setup 6.")
    parser.add_argument("--skip-installer", action="store_true", help="Gera só o aplicativo.")
    parser.add_argument(
        "--skip-self-check", action="store_true", help="Não executa o diagnóstico do executável."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return build(args)
    except BuildError as exc:
        print(f"[build] ERRO: {exc}", file=sys.stderr, flush=True)
        return exc.code
    except KeyboardInterrupt:
        print("[build] Interrompido.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
