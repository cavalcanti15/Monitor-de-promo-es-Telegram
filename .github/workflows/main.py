# --------------------------------------------------------------
# main.py – Monitor de promoções do Telegram (Flet + Telethon)
# --------------------------------------------------------------
import os
import re
import json
import asyncio
import traceback
from typing import Optional

import flet as ft
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
import plyer  # pip install plyer   <-- necessário para notificações nativas

# ------------------------------------------------------------------
# ✅ VARIÁVEIS DO NÍVEL SUPERIOR DE `main`
#    Estes objetos serão “compartilhados” pelas funções aninhadas
#    através da palavra‑chave `nonlocal`. Colocá‑las aqui fora
#    das funções aninhadas resolve o erro SyntaxError.
# ------------------------------------------------------------------
client: Optional[TelegramClient] = None
phone_code_hash: Optional[str] = None

# ------------------------------------------------------------------
# Funções auxiliares (fora de `main` – podem ser reutilizadas)
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


def carregar_config() -> dict:
    storage = Path(os.getenv("FLET_APP_STORAGE_DATA", Path.home()))
    storage.mkdir(parents=True, exist_ok=True)
    cfg_file = storage / "config_monitor.json"
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
    storage = Path(os.getenv("FLET_APP_STORAGE_DATA", Path.home()))
    storage.mkdir(parents=True, exist_ok=True)
    with open(storage / "config_monitor.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
async def main(page: ft.Page):
    # ------------------------------------------------------------------
    # ✅ Inicializar as variáveis “nonlocal” aqui (antes de qualquer
    #    função aninhada). Qualquer alteração feita dentro das funções
    #    `on_ligar_click`, `on_confirmar_codigo_click`, etc. será
    #    refletida nessas variáveis.
    # ------------------------------------------------------------------
    global client, phone_code_hash  # <-- **importante** para a primeira atribuição
    client = None
    phone_code_hash = None

    # ------------------------------------------------------------------
    # Carregar configuração guardada
    # ------------------------------------------------------------------
    cfg = carregar_config()

    # ------------------------------------------------------------------
    # Elementos da UI (conexão)
    # ------------------------------------------------------------------
    txt_status = ft.Text("Status: Desligado", size=18, weight=ft.FontWeight.BOLD, color=ft.colors.RED_700)
    txt_contador = ft.Text("Alertas enviados nesta sessão: 0", size=14, color=ft.colors.GREY_700)
    txt_erro_detalhe = ft.Text("", size=12, color=ft.colors.RED_800, selectable=True)

    inp_api_id = ft.TextField(label="API ID", value=str(cfg.get("api_id", "")), keyboard_type=ft.KeyboardType.NUMBER)
    inp_api_hash = ft.TextField(label="API HASH", value=cfg.get("api_hash", ""))
    inp_tel = ft.TextField(label="Seu Telefone (com +55 e DDD)", value=cfg.get("telefone", "+55"), keyboard_type=ft.KeyboardType.PHONE)

    inp_codigo = ft.TextField(label="Código recebido no Telegram", visible=False, keyboard_type=ft.KeyboardType.NUMBER)
    inp_senha_2fa = ft.TextField(label="Senha de 2 etapas (se tiver)", visible=False, password=True, can_reveal_password=True)

    btn_ligar = ft.ElevatedButton("▶️ Ligar Monitor", on_click=None, bgcolor=ft.colors.GREEN_700, color=ft.colors.WHITE, height=48, width=400)
    btn_confirmar_codigo = ft.ElevatedButton("✅ Confirmar Código", on_click=None, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=48, width=400, visible=False)
    btn_desligar = ft.ElevatedButton("⏹️ Desligar Monitor", on_click=None, bgcolor=ft.colors.RED_700, color=ft.colors.WHITE, height=48, width=400, visible=False)

    # ------------------------------------------------------------------
    # Preferências de notificação (popup, som, vibração)
    # ------------------------------------------------------------------
    chk_popup = ft.Checkbox(label="Mostrar popup", value=cfg.get("notifications", {}).get("popup", True))
    drp_som = ft.Dropdown(
        label="Som",
        options=[ft.dropdown.Option("default"), ft.dropdown.Option("none"), ft.dropdown.Option("custom")],
        value=cfg.get("notifications", {}).get("som", "default"),
    )
    chk_vibrar = ft.Checkbox(label="Vibração", value=cfg.get("notifications", {}).get("vibrate", True))

    def salvar_prefs_notificacoes(e):
        nova_cfg = carregar_config()
        nova_cfg["notifications"] = {"popup": chk_popup.value, "som": drp_som.value, "vibrate": chk_vibrar.value}
        salvar_config(nova_cfg)

    # ------------------------------------------------------------------
    # Área de regras (igual ao seu código original, agora com suporte a
    # notificações nativas quando uma regra bate)
    # ------------------------------------------------------------------
    inp_nome_regra = ft.TextField(label="Nome do Produto (ex: Air Fryer)")
    inp_obrig = ft.TextField(label="Palavras obrigatórias (ex: air fryer, mondial)")
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
                # também remover da nuvem Firebase se estiver configurado (opcional)
                renderizar_regras()
                txt_erro_detalhe.value = "Regra removida!"
                page.update()

            card = ft.Card(
                content=ft.ListTile(
                    title=ft.Text(r["name"], weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(f"Palavras: {obrig_txt}{alt_txt}"),
                    trailing=ft.IconButton(icon=ft.icons.DELETE, icon_color=ft.colors.RED_600, on_click=remover_fn),
                )
            )
            col_lista_regras.controls.append(card)
        page.update()

    def on_add_regra(e):
        nome = inp_nome_regra.value.strip()
        obrig = [t.strip() for t in inp_obrig.value.split(",") if t.strip()]
        alt_raw = inp_alt.value.strip()
        alts = [[t.strip() for t in alt_raw.split(",") if t.strip()]] if alt_raw else []

        if not nome or not obrig:
            txt_erro_detalhe.value = "Preencha o Nome e pelo menos uma palavra!"
            page.update()
            return

        # evita duplicata pelo nome
        cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != nome.lower()]
        cfg["regras"].append({"name": nome, "all": obrig, "any_groups": alts})
        salvar_config(cfg)

        inp_nome_regra.value = ""
        inp_obrig.value = ""
        inp_alt.value = ""
        renderizar_regras()
        txt_erro_detalhe.value = f"Regra '{nome}' salva!"
        page.update()

    btn_add_regra = ft.ElevatedButton("➕ Salvar Regra", on_click=on_add_regra, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=45, width=400)

    # ------------------------------------------------------------------
    # Área de grupos (checkboxes)
    # ------------------------------------------------------------------
    col_lista_grupos = ft.Column(spacing=4)

    async def on_carregar_grupos(e):
        if not client or not monitorando:   # monitorando será definido mais abaixo
            txt_erro_detalhe.value = "Ligue o monitor primeiro!"
            page.update()
            return
        txt_erro_detalhe.value = "A carregar grupos..."
        page.update()
        col_lista_grupos.controls.clear()
        try:
            async for d in client.iter_dialogs():
                if d.is_group or d.is_channel:
                    gid = utils.get_peer_id(d.id)
                    nome = d.name
                    def on_check(ev, gid=gid):
                        if ev.control.value:
                            if gid not in cfg["grupos"]:
                                cfg["grupos"].append(gid)
                        else:
                            cfg["grupos"] = [g for g in cfg["grupos"] if g != gid]
                        salvar_config(cfg)
                    cb = ft.Checkbox(label=nome, value=(gid in cfg["grupos"]), on_change=on_check)
                    col_lista_grupos.controls.append(cb)
        except Exception as ex:
            txt_erro_detalhe.value = traceback.format_exc()
        page.update()
        txt_erro_detalhe.value = f"{len(col_lista_grupos.controls)} grupos carregados!"
        page.update()

    btn_carregar_grupos = ft.ElevatedButton("🔄 Carregar Meus Grupos", on_click=on_carregar_grupos, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=45, width=400)

    # ------------------------------------------------------------------
    # Variáveis de estado da sessão
    # ------------------------------------------------------------------
    monitorando = False
    alertas_enviados = 0

    # ------------------------------------------------------------------
    # Funções que utilizam `nonlocal client, phone_code_hash`
    # ------------------------------------------------------------------
    async def on_ligar_click(e):
        nonlocal client, phone_code_hash  # ✅ agora está ok porque as variáveis existem em `main`

        txt_erro_detalhe.value = ""
        api_id_str = inp_api_id.value.strip()
        api_hash = inp_api_hash.value.strip()
        tel = inp_tel.value.strip()

        if not api_id_str.isdigit() or not api_hash or len(tel) < 10:
            txt_erro_detalhe.value = "Preencha API ID, API HASH e telefone com +55."
            txt_erro_detalhe.color = ft.colors.RED_700
            page.update()
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
                # iniciar serviço em segundo plano (WorkManager) – opcional
                global monitorando
                monitorando = True
                txt_status.value = "Status: Monitorando ✅"
                txt_status.color = ft.colors.GREEN_700
                btn_ligar.visible = False
                btn_confirmar_codigo.visible = False
                inp_codigo.visible = False
                inp_senha_2fa.visible = False
                btn_desligar.visible = True
                page.update()
                txt_erro_detalhe.value = ""
                await iniciar_escuta(client)
            else:
                sent = await client.send_code_request(tel)
                phone_code_hash = sent.phone_code_hash   # ← guarda o hash para usar depois
                inp_codigo.visible = True
                btn_confirmar_codigo.visible = True
                btn_ligar.visible = False
                page.update()
                txt_erro_detalhe.value = "Digite o código que chegou no seu Telegram!"
        except Exception as ex:
            txt_erro_detalhe.value = traceback.format_exc()
            txt_erro_detalhe.color = ft.colors.RED_700
            txt_status.value = "Status: Desligado"
            txt_status.color = ft.colors.RED_700
            btn_ligar.disabled = False
            btn_ligar.text = "▶️ Ligar Monitor"
            page.update()
            txt_erro_detalhe.value = f"Erro ao conectar: {ex}"

    async def on_confirmar_codigo_click(e):
        nonlocal client, phone_code_hash  # ✅ também OK

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
                    txt_erro_detalhe.value = "Sua conta tem senha de 2 etapas. Digite abaixo."
                    page.update()
                    return
                await client.sign_in(password=senha)

            cfg["session_string"] = client.session.save()
            salvar_config(cfg)
            global monitorando
            monitorando = True
            txt_status.value = "Status: Monitorando ✅"
            txt_status.color = ft.colors.GREEN_700
            btn_ligar.visible = False
            btn_confirmar_codigo.visible = False
            inp_codigo.visible = False
            inp_senha_2fa.visible = False
            btn_desligar.visible = True
            page.update()
            txt_erro_detalhe.value = ""
            await iniciar_escuta(client)
        except Exception as ex:
            txt_erro_detalhe.value = traceback.format_exc()
            txt_erro_detalhe.color = ft.colors.RED_700
            txt_status.value = "Status: Desligado"
            txt_status.color = ft.colors.RED_700
            btn_ligar.disabled = False
            btn_ligar.text = "▶️ Ligar Monitor"
            page.update()
            txt_erro_detalhe.value = f"Código ou senha inválidos: {ex}"
        finally:
            btn_confirmar_codigo.disabled = False
            page.update()

    async def on_desligar_click(e):
        global monitorando
        monitorando = False
        txt_status.value = "Status: Desligado"
        txt_status.color = ft.colors.RED_700
        btn_ligar.visible = True
        btn_desligar.visible = False
        page.update()
        # cancelar tarefa de escuta se existir
        if listen_task and not listen_task.done():
            listen_task.cancel()
        if client:
            await client.disconnect()
        txt_erro_detalhe.value = "Monitor desligado."
        page.update()
        txt_erro_detalhe.color = ft.colors.GREY_800

    btn_desligar.on_click = on_desligar_click

    # ------------------------------------------------------------------
    # Função que realmente fica “ouvindo” as mensagens do Telegram
    # ------------------------------------------------------------------
    listen_task: Optional[asyncio.Task] = None

    async def iniciar_escuta(tg_client: TelegramClient):
        nonlocal monitorando, listen_task

        # --- Comandos via "Mensagens Salvas" ---
        @tg_client.on(events.NewMessage(chats="me"))
        async def comando_mensagens_salvas(event):
            texto = (event.raw_text or "").strip()
            if not texto.startswith("/"):
                return
            partes = texto.split(maxsplit=1)
            cmd = partes[0].lower()
            arg = partes[1].strip() if len(partes) > 1 else ""

            # ---- comandos auxiliares ----
            if cmd in ("/ajuda", "/help"):
                await event.reply(
                    "🤖 **Comandos do Monitor de Promoções**\n\n"
                    "• `/regras` — Lista regras ativas\n"
                    "• `/add Nome | obrig1, obrig2 | alt1, alt2` — Adiciona/edita regra\n"
                    "• `/del Nome` — Remove uma regra\n"
                    "• `/grupos` — Mostra grupos filtrados\n"
                    "• `/meusgrupos` — Lista seus grupos/canais e IDs\n"
                    "• `/addgrupo id_ou_user` — Filtra um grupo específico\n"
                    "• `/delgrupo id_ou_user` — Remove grupo do filtro\n"
                    "• `/limpargrupos` — Volta a monitorar TODOS os grupos\n"
                    "• `/testar frase` — Testa uma frase nas regras\n"
                    "• `/status` — Verifica se o monitor está rodando"
                )

            elif cmd == "/status":
                await event.reply(
                    f"✅ **Monitor de Promoções ativo!**\n"
                    f"• Regras ativas: {len(cfg['regras'])}\n"
                    f"• Grupos filtrados: {len(cfg['grupos']) if cfg['grupos'] else 'Todos'}\n"
                    f"• Alertas enviados nesta sessão: {alertas_enviados}"
                )

            elif cmd in ("/regras", "/lista"):
                if not cfg["regras"]:
                    await event.reply("Nenhuma regra cadastrada.")
                else:
                    linhas = []
                    for r in cfg["regras"]:
                        obrig = ", ".join(r["all"])
                        alts = f" | alt: {', '.join(r['any_groups'][0])}" if r.get("any_groups") else ""
                        linhas.append(f"• **{r['name']}**: {obrig}{alts}")
                    await event.reply("📋 **Regras ativas:**\n" + "\n".join(linhas))

            elif cmd == "/add":
                blocos = [b.strip() for b in arg.split("|")]
                if len(blocos) < 2 or not blocos[0] or not blocos[1]:
                    await event.reply("Uso: `/add Nome | obrig1, obrig2 | alt1, alt2`")
                    return
                nome = blocos[0]
                obrig = [t.strip() for t in blocos[1].split(",") if t.strip()]
                alts = [[t.strip() for t in blocos[2].split(",") if t.strip()]] if len(blocos) > 2 and blocos[2] else []
                cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != nome.lower()]
                cfg["regras"].append({"name": nome, "all": obrig, "any_groups": alts})
                salvar_config(cfg)
                renderizar_regras()
                await event.reply(f"✅ Regra **{nome}** adicionada/atualizada!")

            elif cmd == "/del":
                if not arg:
                    await event.reply("Uso: `/del Nome`")
                    return
                antes = len(cfg["regras"])
                cfg["regras"] = [r for r in cfg["regras"] if r["name"].lower() != arg.lower()]
                salvar_config(cfg)
                renderizar_regras()
                if len(cfg["regras"]) < antes:
                    await event.reply(f"🗑️ Regra **{arg}** removida.")
                else:
                    await event.reply(f"Regra **{arg}** não encontrada.")

            elif cmd == "/grupos":
                if not cfg["grupos"]:
                    await event.reply("🌐 Monitorando **TODOS** os grupos e canais.")
                else:
                    lista = "\n".join(f"• `{g}`" for g in cfg["grupos"])
                    await event.reply(f"📌 **Grupos filtrados ({len(cfg['grupos'])}):**\n{lista}")

            elif cmd == "/meusgrupos":
                linhas = []
                async for d in tg_client.iter_dialogs():
                    if d.is_group or d.is_channel:
                        pid = utils.get_peer_id(d.id)
                        user = f" (@{d.entity.username})" if getattr(d.entity, "username", None) else ""
                        linhas.append(f"• **{d.name}**{user} → `{pid}`")
                if not linhas:
                    await event.reply("Nenhum grupo ou canal encontrado.")
                else:
                    bloco = "\n".join(linhas[:50])
                    await event.reply(f"📂 **Seus grupos/canais:**\n{bloco}")

            elif cmd == "/addgrupo":
                if not arg:
                    await event.reply("Uso: `/addgrupo id_ou_user`")
                    return
                try:
                    entidade = await tg_client.get_entity(int(arg) if arg.lstrip("-").isdigit() else arg)
                    pid = utils.get_peer_id(entidade)
                    if pid not in cfg["grupos"]:
                        cfg["grupos"].append(pid)
                        salvar_config(cfg)
                    nome_ent = getattr(entidade, "title", None) or getattr(entidade, "username", str(pid))
                    await event.reply(f"✅ Grupo **{nome_ent}** (`{pid}`) adicionado ao filtro.")
                except Exception as ex:
                    await event.reply(f"❌ Não encontrei o grupo `{arg}`: {ex}")

            elif cmd == "/delgrupo":
                if not arg:
                    await event.reply("Uso: `/delgrupo id_ou_user`")
                    return
                try:
                    entidade = await tg_client.get_entity(int(arg) if arg.lstrip("-").isdigit() else arg)
                    pid = utils.get_peer_id(entidade)
                    alvo_ids = {pid, getattr(entidade, "id", pid)}
                except Exception:
                    alvo_ids = {int(arg)} if arg.lstrip("-").isdigit() else set()
                antes = len(cfg["grupos"])
                cfg["grupos"] = [g for g in cfg["grupos"] if g not in alvo_ids]
                salvar_config(cfg)
                if len(cfg["grupos"]) < antes:
                    await event.reply(f"🗑️ Grupo `{arg}` removido do filtro.")
                else:
                    await event.reply(f"Grupo `{arg}` não estava na lista de filtro.")

            elif cmd == "/limpargrupos":
                cfg["grupos"] = []
                salvar_config(cfg)
                await event.reply("🌐 Filtro limpo! Voltando a monitorar **TODOS** os grupos.")

            elif cmd == "/testar":
                if not arg:
                    await event.reply("Uso: `/testar frase de exemplo`")
                    return
                batidas = checar_regras(arg, cfg["regras"])
                if batidas:
                    await event.reply(f"🎯 **Bateu nas regras:** {', '.join(batidas)}")
                else:
                    await event.reply("❌ Nenhuma regra bateu com essa frase.")

        # ------------------------------------------------------------------
        # Handler de mensagens de grupos (com notificação nativa)
        # ------------------------------------------------------------------
        @tg_client.on(events.NewMessage())
        async def handler_grupos(event):
            nonlocal alertas_enviados
            if event.out or event.is_private:
                return
            # Aplicar filtro de grupos se houver
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

                # --- NOTIFICAÇÃO NATIVA (plyer) ---
                notif_cfg = carregar_config().get("notifications", {})
                if notif_cfg.get("popup", True):
                    som = notif_cfg.get("som", "default")
                    vibrar = notif_cfg.get("vibrate", True)
                    # Lança a notificação em thread separada para não travar o asyncio
                    def show_notif():
                        plyer.notification.notify(
                            title="Promoção detectada!",
                            message=alerta,
                            app_name="MonitorPromocoes",
                            timeout=10,
                            sound=True if som != "none" else None,
                        )
                    asyncio.create_task(show_notif())

                # Também envia para as Mensagens Salvas do Telegram (original)
                await tg_client.send_message("me", alerta)
                alertas_enviados += 1
                txt_contador.value = f"Alertas enviados nesta sessão: {alertas_enviados}"
                page.update()

        # ------------------------------------------------------------------
        # Quando a escuta começa – atualiza UI
        # ------------------------------------------------------------------
        global monitorando
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
        txt_erro_detalhe.value = "Monitor ativo! Envie /ajuda nas Mensagens Salvas."
        page.update()

        # Mantém o cliente rodando até ser desconectado
        listen_task = asyncio.create_task(tg_client.run_until_disconnected())

    # ------------------------------------------------------------------
    # Ligação dos botões
    # ------------------------------------------------------------------
    btn_ligar.on_click = on_ligar_click
    btn_confirmar_codigo.on_click = on_confirmar_codigo_click

    # ------------------------------------------------------------------
    # Tutorial “Como pegar API ID e API HASH”
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Abas de navegação (BottomNavigationBar)
    # ------------------------------------------------------------------
    view_conexao = ft.Column(
        [
            ft.Container(
                padding=16,
                border_radius=12,
                bgcolor=ft.colors.BLUE_GREY_50,
                content=ft.Column([txt_status, txt_contador]),
            ),
            tutorial_box,
            ft.Column([inp_api_id, inp_api_hash, inp_tel, inp_codigo, inp_senha_2fa, btn_ligar, btn_confirmar_codigo, btn_desligar, txt_erro_detalhe], spacing=12),
            ft.Row([chk_popup, drp_som, chk_vibrar], alignment=ft.MainAxisAlignment.CENTER),
        ],
        spacing=12,
    )

    view_regras = ft.Column(
        [
            ft.Text("Criar Novo Alerta", size=18, weight=ft.FontWeight.BOLD),
            inp_nome_regra,
            inp_obrig,
            inp_alt,
            btn_add_regra,
            ft.Divider(),
            ft.Text("Meus Alertas Ativos", size=16, weight=ft.FontWeight.BOLD),
            col_lista_regras,
        ],
        spacing=10,
    )

    view_grupos = ft.Column(
        [
            ft.Text("Escolher Grupos", size=18, weight=ft.FontWeight.BOLD),
            ft.Text("Se nenhum grupo estiver marcado, o app vigia TODOS os seus grupos.", size=13, color=ft.colors.GREY_700),
            btn_carregar_grupos,
            col_lista_grupos,
        ],
        spacing=10,
    )

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

    conteudo = ft.Container(content=view_conexao)

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


# ----------------------------------------------------------------------
# Ponto de entrada do Flet (Android será construído a partir deste target)
# ----------------------------------------------------------------------
ft.app(target=main, view=ft.WEB_BROWSER)   # alterar para `ft.ANDROID_ADK` ao gerar APK via CLI
