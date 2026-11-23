# --------------------------------------------------------------
# main.py – Monitor de promoções do Telegram (Flet + Telethon)
# --------------------------------------------------------------
import os
import re
import json
import asyncio
import traceback
from pathlib import Path

import flet as ft
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

try:
    import plyer
except Exception:
    plyer = None

# ------------------------------------------------------------------
# Funções auxiliares
# ------------------------------------------------------------------
_TROCA_ACENTOS = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
_RE_NUM_UNIDADE = re.compile(r"(\d)\s+(gb|tb|mb|kb)\b")


def normalizar(texto: str) -> str:
    texto = texto.lower().translate(_TROCA_ACENTOS)
    return _RE_NUM_UNIDADE.sub(r"\1\2", texto)


def checar_regras(texto: str, regras: list) -> list:
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


def _pasta_config() -> Path:
    storage = Path(os.getenv("FLET_APP_STORAGE_DATA", str(Path.home())))
    storage.mkdir(parents=True, exist_ok=True)
    return storage


def carregar_config() -> dict:
    cfg_file = _pasta_config() / "config_monitor.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_id": 0,
        "api_hash": "",
        "telefone": "+55",
        "session_string": "",
        "grupos": [],
        "regras": [],
        "notifications": {"popup": True, "som": "default", "vibrate": True},
    }


def salvar_config(cfg: dict):
    with open(_pasta_config() / "config_monitor.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# Função principal do Flet
# ------------------------------------------------------------------
async def main(page: ft.Page):
    page.title = "Monitor de Promoções"
    page.scroll = ft.ScrollMode.AUTO

    # ---- Estado (tudo local a `main`; as funções internas usam nonlocal) ----
    client = None
    phone_code_hash = None
    monitorando = False
    listen_task = None
    alertas_enviados = 0
    handlers_registrados = []   # guarda (callback, evento) para remover depois

    cfg = carregar_config()

    # ---------- UI (conexão) ----------
    txt_status = ft.Text("Status: Desligado", size=18, weight=ft.FontWeight.BOLD, color=ft.colors.RED_700)
    txt_contador = ft.Text("Alertas enviados nesta sessão: 0", size=14, color=ft.colors.GREY_700)
    txt_erro_detalhe = ft.Text("", size=12, color=ft.colors.RED_800, selectable=True)

    inp_api_id = ft.TextField(label="API ID", value=str(cfg.get("api_id", "")), keyboard_type=ft.KeyboardType.NUMBER)
    inp_api_hash = ft.TextField(label="API HASH", value=cfg.get("api_hash", ""))
    inp_tel = ft.TextField(label="Seu Telefone (com +55 e DDD)", value=cfg.get("telefone", "+55"), keyboard_type=ft.KeyboardType.PHONE)

    inp_codigo = ft.TextField(label="Código recebido no Telegram", visible=False, keyboard_type=ft.KeyboardType.NUMBER)
    inp_senha_2fa = ft.TextField(label="Senha de 2 etapas (se tiver)", visible=False, password=True, can_reveal_password=True)

    btn_ligar = ft.ElevatedButton("▶️ Ligar Monitor", bgcolor=ft.colors.GREEN_700, color=ft.colors.WHITE, height=48, width=400)
    btn_confirmar_codigo = ft.ElevatedButton("✅ Confirmar Código", bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=48, width=400, visible=False)
    btn_desligar = ft.ElevatedButton("⏹️ Desligar Monitor", bgcolor=ft.colors.RED_700, color=ft.colors.WHITE, height=48, width=400, visible=False)

    # ---------- Preferências de notificação ----------
    notif = cfg.get("notifications", {})
    chk_popup = ft.Checkbox(label="Popup", value=notif.get("popup", True))
    drp_som = ft.Dropdown(
        label="Som",
        width=140,
        options=[ft.dropdown.Option("default"), ft.dropdown.Option("none"), ft.dropdown.Option("custom")],
        value=notif.get("som", "default"),
    )
    chk_vibrar = ft.Checkbox(label="Vibração", value=notif.get("vibrate", True))

    def salvar_prefs_notificacoes(e):
        cfg["notifications"] = {"popup": chk_popup.value, "som": drp_som.value, "vibrate": chk_vibrar.value}
        salvar_config(cfg)

    chk_popup.on_change = salvar_prefs_notificacoes
    drp_som.on_change = salvar_prefs_notificacoes
    chk_vibrar.on_change = salvar_prefs_notificacoes

    # ---------- Regras ----------
    inp_nome_regra = ft.TextField(label="Nome do Produto (ex: Air Fryer)")
    inp_obrig = ft.TextField(label="Palavras obrigatórias (ex: air fryer, municipal)")
    inp_alt = ft.TextField(label="Opções alternativas - opcional (ex: 110v, 220v, 4l)")
    col_lista_regras = ft.Column(spacing=8)

    def renderizar_regras():
        col_lista_regras.controls.clear()
        regras = cfg.get("regras", [])
        if not regras:
            col_lista_regras.controls.append(ft.Text("Nenhuma regra criada ainda.", italic=True, color=ft.colors.GREY_600))
        for idx, r in enumerate(regras):
            obrig_txt = ", ".join(r.get("all", []))
            alt_txt = f" | Opções: {', '.join(r.get('any_groups', [])[0])}" if r.get("any_groups") else ""

            def remover_fn(e, i=idx):
                cfg["regras"].pop(i)
                salvar_config(cfg)
                renderizar_regras()
                txt_erro_detalhe.value = "Regra removida!"
                page.update()

            col_lista_regras.controls.append(
                ft.Card(
                    content=ft.ListTile(
                        title=ft.Text(r["name"], weight=ft.FontWeight.BOLD),
                        subtitle=ft.Text(f"Palavras: {obrig_txt}{alt_txt}"),
                        trailing=ft.IconButton(icon=ft.icons.DELETE, icon_color=ft.colors.RED_600, on_click=remover_fn),
                    )
                )
            )
        page.update()

    def on_add_regra(e):
        nome = inp_nome_regra.value.strip()
        obrig = [t.strip() for t in inp_obrig.value.split(",") if t.strip()]
        alt_raw = inp_alt.value.strip()
        alts = [[t.strip() for t in alt_raw.split(",") if t.strip()]] if
