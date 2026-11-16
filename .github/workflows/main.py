import os
import re
import json
import asyncio
import flet as ft
from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError

# Opcional: você pode pré-preencher um API_ID / API_HASH padrão aqui para que
# o usuário só precise digitar o telefone e o código do Telegram.
DEFAULT_API_ID = ""
DEFAULT_API_HASH = ""

STORAGE_DIR = os.getenv("FLET_APP_STORAGE_DATA", ".")
os.makedirs(STORAGE_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(STORAGE_DIR, "config_monitor.json")
SESSION_FILE = os.path.join(STORAGE_DIR, "sessao_usuario")

# ============ LÓGICA DE REGRAS ============

_TROCA_ACENTOS = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
_RE_NUM_UNIDADE = re.compile(r"(\d)\s+(gb|tb|mb|kb)\b")


def normalizar(texto: str) -> str:
    texto = texto.lower().translate(_TROCA_ACENTOS)
    return _RE_NUM_UNIDADE.sub(r"\1\2", texto)


def checar_regras(texto: str, regras: list[dict]) -> list[str]:
    texto_norm = normalizar(texto)
    batidas = []
    for r in regras:
        obrig = [normalizar(t) for t in r.get("all", []) if t.strip()]
        if not obrig or not all(t in texto_norm for t in obrig):
            continue
        passou = True
        for grupo in r.get("any_groups", []):
            grupo_norm = [normalizar(t) for t in grupo if t.strip()]
            if grupo_norm and not any(t in texto_norm for t in grupo_norm):
                passou = False
                break
        if passou:
            batidas.append(r["name"])
    return batidas


def carregar_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_id": DEFAULT_API_ID,
        "api_hash": DEFAULT_API_HASH,
        "telefone": "+55",
        "grupos": [],
        "regras": [],
    }


def salvar_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============ INTERFACE ANDROID (FLET) ============

async def main(page: ft.Page):
    page.title = "Monitor de Promoções"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 16
    page.scroll = ft.ScrollMode.AUTO

    cfg = carregar_config()
    client: TelegramClient | None = None
    monitorando = False
    phone_code_hash = None

    def avisar(msg: str, cor: str = ft.colors.BLUE_700):
        page.snack_bar = ft.SnackBar(ft.Text(msg, color=ft.colors.WHITE), bgcolor=cor)
        page.snack_bar.open = True
        page.update()

    # --- ELEMENTOS DA ABA 1: CONEXÃO ---
    txt_status = ft.Text("Status: Desligado", size=18, weight=ft.FontWeight.BOLD, color=ft.colors.RED_700)
    txt_contador = ft.Text("Alertas enviados nesta sessão: 0", size=14, color=ft.colors.GREY_700)
    alertas_enviados = 0

    inp_api_id = ft.TextField(label="API ID", value=str(cfg.get("api_id", "")), keyboard_type=ft.KeyboardType.NUMBER)
    inp_api_hash = ft.TextField(label="API HASH", value=cfg.get("api_hash", ""))
    inp_tel = ft.TextField(label="Seu Telefone (com +55 e DDD)", value=cfg.get("telefone", "+55"), keyboard_type=ft.KeyboardType.PHONE)

    inp_codigo = ft.TextField(label="Código recebido no Telegram", visible=False, keyboard_type=ft.KeyboardType.NUMBER)
    inp_senha_2fa = ft.TextField(label="Senha de 2 etapas (se tiver)", visible=False, password=True, can_reveal_password=True)

    async def iniciar_escuta(tg_client: TelegramClient):
