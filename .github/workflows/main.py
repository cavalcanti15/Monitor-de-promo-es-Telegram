import os
import re
import json
import asyncio
import traceback
import flet as ft
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

DEFAULT_API_ID = ""
DEFAULT_API_HASH = ""

STORAGE_DIR = os.getenv("FLET_APP_STORAGE_DATA") or os.path.expanduser("~")
os.makedirs(STORAGE_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(STORAGE_DIR, "config_monitor.json")

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
        "session_string": "",
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
    page.padding = ft.padding.only(top=36, left=16, right=16, bottom=16)
    page.scroll = ft.ScrollMode.AUTO

    cfg = carregar_config()
    client: TelegramClient | None = None
    listen_task: asyncio.Task | None = None
    monitorando = False
    phone_code_hash = None
    alertas_enviados = 0

    def avisar(msg: str, cor: str = ft.colors.BLUE_700):
        page.snack_bar = ft.SnackBar(ft.Text(msg, color=ft.colors.WHITE), bgcolor=cor)
        page.snack_bar.open = True
        page.update()

    # --- ELEMENTOS DA ABA 1: CONEXÃO ---
    txt_status = ft.Text("Status: Desligado", size=18, weight=ft.FontWeight.BOLD, color=ft.colors.RED_700)
    txt_contador = ft.Text("Alertas enviados nesta sessão: 0", size=14, color=ft.colors.GREY_700)
    txt_erro_detalhe = ft.Text("", size=12, color=ft.colors.RED_800, selectable=True)

    inp_api_id = ft.TextField(label="API ID", value=str(cfg.get("api_id", "")), keyboard_type=ft.KeyboardType.NUMBER)
    inp_api_hash = ft.TextField(label="API HASH", value=cfg.get("api_hash", ""))
    inp_tel = ft.TextField(label="Seu Telefone (com +55 e DDD)", value=cfg.get("telefone", "+55"), keyboard_type=ft.KeyboardType.PHONE)

    inp_codigo = ft.TextField(label="Código recebido no Telegram", visible=False, keyboard_type=ft.KeyboardType.NUMBER)
    inp_senha_2fa = ft.TextField(label="Senha de 2 etapas (se tiver)", visible=False, password=True, can_reveal_password=True)

    # --- ELEMENTOS DA ABA 2: MINHAS REGRAS ---
    inp_nome_regra = ft.TextField(label="Nome do Produto (ex: Air Fryer)")
    inp_obrig = ft.TextField(label="Palavras obrigatórias (ex: air fryer, mondial)")
    inp_alt = ft.TextField(label="Opções alternativas - opcional (ex: 110v, 220v, 4l)")
    col_lista_regras = ft.Column(spacing=8)

    def renderizar_regras():
        col_lista_regras.controls.clear()
        if not cfg["regras"]:
            col_lista_regras.controls.append(ft.Text("Nenhuma regra criada ainda.", italic=True, color=ft.colors.GREY_600))
        for idx, r in enumerate(cfg["regras"]):
            obrig_txt = ", ".join(r["all"])
            alt_txt = f" | Opções: {', '.join(r['any_groups'][0])}" if r.get("any_groups") else ""

            def remover_fn(e, i=idx):
                cfg["regras"].pop(i)
                salvar_config(cfg)
                renderizar_regras()
                avisar("Regra removida!")

            card = ft.Card(
                content=ft.ListTile(
                    title=ft.Text(r["name"], weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(f"Palavras: {obrig_txt}{alt_txt}"),
                    trailing=ft.IconButton(icon=ft.icons.DELETE, icon_color=ft.colors.RED_600, on_click=remover_fn),
                )
            )
            col_lista_regras.controls.append(card)
        page.update()

    # --- COMANDOS VIA MENSAGENS SALVAS + ESCUTA DE GRUPOS ---
    async def iniciar_escuta(tg_client: TelegramClient):
        nonlocal monitorando, alertas_enviados, listen_task

        @tg_client.on(events.NewMessage(chats="me"))
        async def comando_mensagens_salvas(event):
            texto = (event.raw_text or "").strip()
            if not texto.startswith("/"):
                return
            partes = texto.split(maxsplit=1)
            cmd = partes[0].lower()
            arg = partes[1].strip() if len(partes) > 1 else ""

            if cmd in ("/ajuda", "/help"):
                await event.reply(
                    "🤖 **Comandos do Monitor:**\n"
                    "`/add Nome | palavra1, palavra2 | alt1, alt2`\n"
                    "`/del Nome` — remove uma regra\n"
                    "`/lista` — lista regras ativas\n"
                    "`/status` — mostra status e alertas"
                )
            elif cmd == "/status":
                await event.reply(
                    f"✅ Monitor ativo!\n"
                    f"Regras: {len(cfg['regras'])}\n"
                    f"Grupos filtrados: {len(cfg['grupos']) or 'Todos'}\n"
                    f"Alertas na sessão: {alertas_enviados}"
                )
            elif cmd == "/lista":
                if not cfg["regras"]:
                    await event.reply("Nenhuma regra cadastrada.")
                else:
                    linhas = [f"• **{r['name']}**: {', '.join(r['all'])}" for r in cfg["regras"]]
                    await event.reply("📋 **Regras ativas:**\n" + "\n".join(linhas))
            elif cmd == "/add":
                blocos = [b.strip() for b in arg.split("|")]
                if len(blocos) < 2 or not blocos[0] or not blocos[1]:
                    await event.reply("Uso: `/add Nome | palavra1, palavra2 | opcional1, opcional2`")
                    return
                nome = blocos[0]
                obrig = [t.strip() for t in blocos[1].split(",") if t.strip()]
                alts = [[t.strip() for t in blocos[2].split(",") if t.strip()]] if len(blocos) > 2 and blocos[2] else []
                cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != nome.lower()]
                cfg["regras"].append({"name": nome, "all": obrig, "any_groups": alts})
                salvar_config(cfg)
                renderizar_regras()
                await event.reply(f"✅ Regra **{nome}** adicionada!")
            elif cmd == "/del":
                antes = len(cfg["regras"])
                cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != arg.lower()]
                salvar_config(cfg)
                renderizar_regras()
                if len(cfg["regras"]) < antes:
                    await event.reply(f"🗑️ Regra **{arg}** removida.")
                else:
                    await event.reply(f"Regra **{arg}** não encontrada.")

        @tg_client.on(events.NewMessage())
        async def handler_grupos(event):
            nonlocal alertas_enviados
            if event.out or event.is_private:
                return
            if cfg["grupos"]:
                peer_id = utils.get_peer_id(event.chat_id)
                if peer_id not in cfg["grupos"] and event.chat_id not in cfg["grupos"]:
                    return
            texto = event.raw_text or ""
            if not texto:
                return
            batidas = checar_regras(texto, cfg["regras"])
            if batidas:
                chat = await event.get_chat()
                nome_chat = getattr(chat, "title", None) or getattr(chat, "username", "Grupo")
                user = getattr(chat, "username", None)
                link = f"\n\n🔗 https://t.me/{user}/{event.id}" if user else ""
                alerta = (
                    f"🔔 **Promoção encontrada!**\n"
                    f"🏷️ **Alerta:** {', '.join(batidas)}\n"
                    f"📍 **Grupo:** {nome_chat}\n\n"
                    f"{texto[:550]}{link}"
                )
                await tg_client.send_message("me", alerta)
                alertas_enviados += 1
                txt_contador.value = f"Alertas enviados nesta sessão: {alertas_enviados}"
                page.update()

        listen_task = asyncio.create_task(tg_client.run_until_disconnected())

        monitorando = True
        txt_erro_detalhe.value = ""
        txt_status.value = "Status: Monitorando ✅"
        txt_status.color = ft.colors.GREEN_700
        btn_ligar.visible = False
        btn_confirmar_codigo.visible = False
        inp_codigo.visible = False
        inp_senha_2fa.visible = False
        btn_desligar.visible = True
        page.update()
        avisar("Monitor ativo! Envie /ajuda nas Mensagens Salvas.", ft.colors.GREEN_700)

    async def on_ligar_click(e):
        nonlocal client, phone_code_hash
        txt_erro_detalhe.value = ""
        api_id_str = inp_api_id.value.strip()
        api_hash = inp_api_hash.value.strip()
        tel = inp_tel.value.strip()

        if not api_id_str.isdigit() or not api_hash or len(tel) < 10:
            avisar("Preencha API ID, API HASH e seu telefone com +55.", ft.colors.RED_700)
            return

        btn_ligar.disabled = True
        btn_ligar.text = "Conectando..."
        page.update()

        cfg["api_id"] = int(api_id_str)
        cfg["api_hash"] = api_hash
        cfg["telefone"] = tel
        salvar_config(cfg)

        try:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            loop = asyncio.get_running_loop()
            sessao = StringSession(cfg.get("session_string", ""))
            client = TelegramClient(sessao, cfg["api_id"], cfg["api_hash"], loop=loop)
            await client.connect()

            if await client.is_user_authorized():
                cfg["session_string"] = client.session.save()
                salvar_config(cfg)
                await iniciar_escuta(client)
            else:
                sent = await client.send_code_request(tel)
                phone_code_hash = sent.phone_code_hash
                inp_codigo.visible = True
                btn_confirmar_codigo.visible = True
                btn_ligar.visible = False
                page.update()
                avisar("Digite o código que chegou no seu Telegram!")
        except Exception as ex:
            txt_erro_detalhe.value = traceback.format_exc()
            avisar(f"Erro ao conectar: {ex}", ft.colors.RED_700)
        finally:
            btn_ligar.disabled = False
            btn_ligar.text = "▶️ Ligar Monitor"
            page.update()

    async def on_confirmar_codigo_click(e):
        codigo = inp_codigo.value.strip()
        senha = inp_senha_2fa.value.strip()
        if not codigo or not client:
            return
        btn_confirmar_codigo.disabled = True
        page.update()
        try:
            try:
                await client.sign_in(cfg["telefone"], codigo, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if not senha:
                    inp_senha_2fa.visible = True
                    page.update()
                    avisar("Sua conta tem senha de 2 etapas. Digite abaixo.")
                    return
                await client.sign_in(password=senha)

            cfg["session_string"] = client.session.save()
            salvar_config(cfg)
            await iniciar_escuta(client)
        except Exception as ex:
            txt_erro_detalhe.value = traceback.format_exc()
            avisar(f"Código ou senha inválidos: {ex}", ft.colors.RED_700)
        finally:
            btn_confirmar_codigo.disabled = False
            page.update()

    async def on_desligar_click(e):
        nonlocal monitorando, listen_task
        if listen_task and not listen_task.done():
            listen_task.cancel()
        if client:
            await client.disconnect()
        monitorando = False
        txt_status.value = "Status: Desligado"
        txt_status.color = ft.colors.RED_700
        btn_ligar.visible = True
        btn_desligar.visible = False
        page.update()
        avisar("Monitor desligado.", ft.colors.GREY_800)

    btn_ligar = ft.ElevatedButton(
        "▶️ Ligar Monitor",
        on_click=on_ligar_click,
        bgcolor=ft.colors.GREEN_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
    )
    btn_confirmar_codigo = ft.ElevatedButton(
        "✅ Confirmar Código",
        on_click=on_confirmar_codigo_click,
        bgcolor=ft.colors.BLUE_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
        visible=False,
    )
    btn_desligar = ft.ElevatedButton(
        "⏹️ Desligar Monitor",
        on_click=on_desligar_click,
        bgcolor=ft.colors.RED_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
        visible=False,
    )

    tutorial_box = ft.ExpansionTile(
        title=ft.Text("❓ Como pegar API ID e API HASH?"),
        controls=[
            ft.Container(
                padding=10,
                content=ft.Text(
                    "1. Acesse my.telegram.org no navegador.\n"
                    "2. Entre com seu número (+55 + DDD) e o código que chegar no Telegram.\n"
                    "3. Toque em 'API development tools'.\n"
                    "4. Crie um app com qualquer nome (ex: Monitor) e copie App api_id e App api_hash.",
                    size=13,
                ),
            )
        ],
    )

    view_conexao = ft.Column(
        [
            ft.Container(
                padding=16,
                border_radius=12,
                bgcolor=ft.colors.BLUE_GREY_50,
                content=ft.Column([txt_status, txt_contador]),
            ),
            tutorial_box,
            inp_api_id,
            inp_api_hash,
            inp_tel,
            inp_codigo,
            inp_senha_2fa,
            btn_ligar,
            btn_confirmar_codigo,
            btn_desligar,
            txt_erro_detalhe,
        ],
        spacing=12,
    )

    def on_add_regra(e):
        nome = inp_nome_regra.value.strip()
        obrig = [t.strip() for t in inp_obrig.value.split(",") if t.strip()]
        alt_raw = inp_alt.value.strip()
        alts = [[t.strip() for t in alt_raw.split(",") if t.strip()]] if alt_raw else []

        if not nome or not obrig:
            avisar("Preencha o Nome e pelo menos uma palavra!", ft.colors.RED_700)
            return

        cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != nome.lower()]
        cfg["regras"].append({"name": nome, "all": obrig, "any_groups": alts})
        salvar_config(cfg)

        inp_nome_regra.value = ""
        inp_obrig.value = ""
        inp_alt.value = ""
        renderizar_regras()
        avisar(f"Regra '{nome}' salva!", ft.colors.GREEN_700)

    view_regras = ft.Column(
        [
            ft.Text("Criar Novo Alerta", size=18, weight=ft.FontWeight.BOLD),
            inp_nome_regra,
            inp_obrig,
            inp_alt,
            ft.ElevatedButton(
                "➕ Salvar Regra",
                on_click=on_add_regra,
                bgcolor=ft.colors.BLUE_700,
                color=ft.colors.WHITE,
                height=45,
                width=400,
            ),
            ft.Divider(),
            ft.Text("Meus Alertas Ativos", size=16, weight=ft.FontWeight.BOLD),
            col_lista_regras,
        ],
        spacing=10,
    )

    # --- ELEMENTOS DA ABA 3: GRUPOS ---
    col_lista_grupos = ft.Column(spacing=4)

    async def on_carregar_grupos(e):
        if not client or not monitorando:
            avisar("Ligue o monitor primeiro na aba 'Conexão'!", ft.colors.RED_700)
            return

        avisar("Buscando seus grupos...")
        col_lista_grupos.controls.clear()
        grupos_encontrados = []

        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                grupos_encontrados.append((d.id, d.name))

        for gid, nome in grupos_encontrados:
            def on_check(ev, group_id=gid):
                if ev.control.value:
                    if group_id not in cfg["grupos"]:
                        cfg["grupos"].append(group_id)
                else:
                    cfg["grupos"] = [g for g in cfg["grupos"] if g != group_id]
                salvar_config(cfg)

            cb = ft.Checkbox(label=nome, value=(gid in cfg["grupos"]), on_change=on_check)
            col_lista_grupos.controls.append(cb)

        page.update()
        avisar(f"{len(grupos_encontrados)} grupos carregados!", ft.colors.GREEN_700)

    view_grupos = ft.Column(
        [
            ft.Text("Escolher Grupos", size=18, weight=ft.FontWeight.BOLD),
            ft.Text("Se nenhum grupo estiver marcado, o app vigia TODOS os seus grupos.", size=13, color=ft.colors.GREY_700),
            ft.ElevatedButton(
                "🔄 Carregar Meus Grupos",
                on_click=on_carregar_grupos,
                bgcolor=ft.colors.BLUE_700,
                color=ft.colors.WHITE,
                height=45,
                width=400,
            ),
            col_lista_grupos,
        ],
        spacing=10,
    )

    # --- NAVEGAÇÃO INFERIOR ---
    conteudo = ft.Container(content=view_conexao)

    def mudar_aba(e):
        idx = e.control.selected_index
        if idx == 0:
            conteudo.content = view_conexao
        elif idx == 1:
            renderizar_regras()
            conteudo.content = view_regras
        else:
            conteudo.content = view_grupos
        page.update()

    page.navigation_bar = ft.NavigationBar(
        selected_index=0,
        on_change=mudar_aba,
        destinations=[
            ft.NavigationBarDestination(icon=ft.icons.POWER_SETTINGS_NEW, label="Conexão"),
            ft.NavigationBarDestination(icon=ft.icons.NOTIFICATIONS_ACTIVE, label="Regras"),
            ft.NavigationBarDestination(icon=ft.icons.GROUPS, label="Grupos"),
        ],
    )

    page.add(conteudo)


ft.app(target=main)
                          
