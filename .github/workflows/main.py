# --------------------------------------------------------------
# main.py - Monitor de promocoes do Telegram (Flet + Telethon)
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

_TROCA_ACENTOS = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
_RE_NUM_UNIDADE = re.compile(r"(\d)\s+(gb|tb|mb|kb)\b")


def normalizar(texto):
    texto = texto.lower().translate(_TROCA_ACENTOS)
    return _RE_NUM_UNIDADE.sub(r"\1\2", texto)


def separar(texto):
    """Transforma 'a, b, c' em ['a', 'b', 'c']."""
    partes = [t.strip() for t in texto.split(",")]
    return [t for t in partes if t]


def checar_regras(texto, regras):
    texto_norm = normalizar(texto)
    batidas = []
    for r in regras:
        obrig = [normalizar(t) for t in r.get("all", []) if t.strip()]
        if not obrig:
            continue
        if not all(t in texto_norm for t in obrig):
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


def _pasta_config():
    base = os.getenv("FLET_APP_STORAGE_DATA", str(Path.home()))
    storage = Path(base)
    storage.mkdir(parents=True, exist_ok=True)
    return storage


def carregar_config():
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


def salvar_config(cfg):
    arq = _pasta_config() / "config_monitor.json"
    with open(arq, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def main(page: ft.Page):
    page.title = "Monitor de Promoções"
    page.scroll = ft.ScrollMode.AUTO

    # Estado (local a main; funcoes internas usam nonlocal)
    client = None
    phone_code_hash = None
    monitorando = False
    listen_task = None
    alertas_enviados = 0
    handlers_registrados = []

    cfg = carregar_config()

    # ---------- UI (conexao) ----------
    txt_status = ft.Text(
        "Status: Desligado",
        size=18,
        weight=ft.FontWeight.BOLD,
        color=ft.colors.RED_700,
    )
    txt_contador = ft.Text(
        "Alertas enviados nesta sessão: 0",
        size=14,
        color=ft.colors.GREY_700,
    )
    txt_erro_detalhe = ft.Text(
        "", size=12, color=ft.colors.RED_800, selectable=True
    )

    inp_api_id = ft.TextField(
        label="API ID",
        value=str(cfg.get("api_id", "")),
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    inp_api_hash = ft.TextField(
        label="API HASH",
        value=cfg.get("api_hash", ""),
    )
    inp_tel = ft.TextField(
        label="Seu Telefone (com +55 e DDD)",
        value=cfg.get("telefone", "+55"),
        keyboard_type=ft.KeyboardType.PHONE,
    )
    inp_codigo = ft.TextField(
        label="Código recebido no Telegram",
        visible=False,
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    inp_senha_2fa = ft.TextField(
        label="Senha de 2 etapas (se tiver)",
        visible=False,
        password=True,
        can_reveal_password=True,
    )

    btn_ligar = ft.ElevatedButton(
        "▶️ Ligar Monitor",
        bgcolor=ft.colors.GREEN_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
    )
    btn_confirmar_codigo = ft.ElevatedButton(
        "✅ Confirmar Código",
        bgcolor=ft.colors.BLUE_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
        visible=False,
    )
    btn_desligar = ft.ElevatedButton(
        "⏹️ Desligar Monitor",
        bgcolor=ft.colors.RED_700,
        color=ft.colors.WHITE,
        height=48,
        width=400,
        visible=False,
    )

    # ---------- Preferencias de notificacao ----------
    notif = cfg.get("notifications", {})
    chk_popup = ft.Checkbox(label="Popup", value=notif.get("popup", True))
    drp_som = ft.Dropdown(
        label="Som",
        width=140,
        options=[
            ft.dropdown.Option("default"),
            ft.dropdown.Option("none"),
            ft.dropdown.Option("custom"),
        ],
        value=notif.get("som", "default"),
    )
    chk_vibrar = ft.Checkbox(
        label="Vibração", value=notif.get("vibrate", True)
    )

    def salvar_prefs_notificacoes(e):
        cfg["notifications"] = {
            "popup": chk_popup.value,
            "som": drp_som.value,
            "vibrate": chk_vibrar.value,
        }
        salvar_config(cfg)

    chk_popup.on_change = salvar_prefs_notificacoes
    drp_som.on_change = salvar_prefs_notificacoes
    chk_vibrar.on_change = salvar_prefs_notificacoes

    # ---------- Regras ----------
    inp_nome_regra = ft.TextField(label="Nome do Produto (ex: Air Fryer)")
    inp_obrig = ft.TextField(
        label="Palavras obrigatórias (ex: air fryer, municipal)"
    )
    inp_alt = ft.TextField(
        label="Opções alternativas - opcional (ex: 110v, 220v, 4l)"
    )
    col_lista_regras = ft.Column(spacing=8)

    def renderizar_regras():
        col_lista_regras.controls.clear()
        regras = cfg.get("regras", [])
        if not regras:
            col_lista_regras.controls.append(
                ft.Text(
                    "Nenhuma regra criada ainda.",
                    italic=True,
                    color=ft.colors.GREY_600,
                )
            )
        for idx, r in enumerate(regras):
            obrig_txt = ", ".join(r.get("all", []))
            alt_txt = ""
            if r.get("any_groups"):
                opcoes = ", ".join(r["any_groups"][0])
                alt_txt = f" | Opções: {opcoes}"

            def remover_fn(e, i=idx):
                cfg["regras"].pop(i)
                salvar_config(cfg)
                renderizar_regras()
                txt_erro_detalhe.value = "Regra removida!"
                page.update()

            tile = ft.ListTile(
                title=ft.Text(r["name"], weight=ft.FontWeight.BOLD),
                subtitle=ft.Text(f"Palavras: {obrig_txt}{alt_txt}"),
                trailing=ft.IconButton(
                    icon=ft.icons.DELETE,
                    icon_color=ft.colors.RED_600,
                    on_click=remover_fn,
                ),
            )
            col_lista_regras.controls.append(ft.Card(content=tile))
        page.update()

    def on_add_regra(e):
        nome = inp_nome_regra.value.strip()
        obrig = separar(inp_obrig.value)
        alt_raw = inp_alt.value.strip()
        alts = []
        if alt_raw:
            alts = [separar(alt_raw)]

        if not nome or not obrig:
            txt_erro_detalhe.value = "Preencha o Nome e pelo menos uma palavra!"
            page.update()
            return

        cfg["regras"] = [
            r for r in cfg["regras"] if r["name"].lower() != nome.lower()
        ]
        cfg["regras"].append(
            {"name": nome, "all": obrig, "any_groups": alts}
        )
        salvar_config(cfg)

        inp_nome_regra.value = ""
        inp_obrig.value = ""
        inp_alt.value = ""
        renderizar_regras()
        txt_erro_detalhe.value = f"Regra '{nome}' salva!"
        page.update()

    btn_add_regra = ft.ElevatedButton(
        "➕ Salvar Regra",
        on_click=on_add_regra,
        bgcolor=ft.colors.BLUE_700,
        color=ft.colors.WHITE,
        height=45,
        width=400,
    )

    # ---------- Grupos ----------
    col_lista_grupos = ft.Column(spacing=4)

    async def on_carregar_grupos(e):
        if not client or not monitorando:
            txt_erro_detalhe.value = "Ligue o monitor primeiro!"
            page.update()
            return
        txt_erro_detalhe.value = "Carregando grupos..."
        page.update()
        col_lista_grupos.controls.clear()
        try:
            async for d in client.iter_dialogs():
                if not (d.is_group or d.is_channel):
                    continue
                gid = utils.get_peer_id(d.entity)

                def on_check(ev, gid=gid):
                    if ev.control.value:
                        if gid not in cfg["grupos"]:
                            cfg["grupos"].append(gid)
                    else:
                        cfg["grupos"] = [
                            g for g in cfg["grupos"] if g != gid
                        ]
                    salvar_config(cfg)

                cb = ft.Checkbox(
                    label=d.name,
                    value=(gid in cfg["grupos"]),
                    on_change=on_check,
                )
                col_lista_grupos.controls.append(cb)
            total = len(col_lista_grupos.controls)
            txt_erro_detalhe.value = f"{total} grupos carregados!"
        except Exception:
            txt_erro_detalhe.value = traceback.format_exc()
        page.update()

    btn_carregar_grupos = ft.ElevatedButton(
        "🔄 Carregar Meus Grupos",
        on_click=on_carregar_grupos,
        bgcolor=ft.colors.BLUE_700,
        color=ft.colors.WHITE,
        height=45,
        width=400,
    )

    # ---------- Helpers de UI ----------
    def ui_monitorando():
        txt_status.value = "Status: Monitorando ✅"
        txt_status.color = ft.colors.GREEN_700
        btn_ligar.visible = False
        btn_confirmar_codigo.visible = False
        inp_codigo.visible = False
        inp_senha_2fa.visible = False
        btn_desligar.visible = True

    def ui_desligado():
        txt_status.value = "Status: Desligado"
        txt_status.color = ft.colors.RED_700
        btn_ligar.visible = True
        btn_ligar.disabled = False
        btn_ligar.text = "▶️ Ligar Monitor"
        btn_desligar.visible = False

    # ---------- Escuta ----------
    async def iniciar_escuta(tg_client):
        nonlocal monitorando, listen_task

        for cb, ev in handlers_registrados:
            tg_client.remove_event_handler(cb, ev)
        handlers_registrados.clear()

        async def comando_mensagens_salvas(event):
            texto = (event.raw_text or "").strip()
            if not texto.startswith("/"):
                return
            partes = texto.split(maxsplit=1)
            cmd = partes[0].lower()
            arg = partes[1].strip() if len(partes) > 1 else ""

            if cmd in ("/ajuda", "/help"):
                await event.reply(
                    "🤖 **Comandos do Monitor de Promoções**\n\n"
                    "• `/regras` — Lista regras ativas\n"
                    "• `/add Nome | obrig1, obrig2 | alt1, alt2`"
                    " — Adiciona/edita regra\n"
                    "• `/del Nome` — Remove uma regra\n"
                    "• `/grupos` — Mostra grupos filtrados\n"
                    "• `/meusgrupos` — Lista seus grupos e IDs\n"
                    "• `/addgrupo id_ou_user` — Filtra um grupo\n"
                    "• `/delgrupo id_ou_user` — Remove do filtro\n"
                    "• `/limpargrupos` — Monitora TODOS os grupos\n"
                    "• `/testar frase` — Testa uma frase\n"
                    "• `/status` — Verifica o monitor"
                )

            elif cmd == "/status":
                n_grupos = len(cfg["grupos"]) if cfg["grupos"] else "Todos"
                await event.reply(
                    "✅ **Monitor de Promoções ativo!**\n"
                    f"• Regras ativas: {len(cfg['regras'])}\n"
                    f"• Grupos filtrados: {n_grupos}\n"
                    f"• Alertas nesta sessão: {alertas_enviados}"
                )

            elif cmd in ("/regras", "/lista"):
                if not cfg["regras"]:
                    await event.reply("Nenhuma regra cadastrada.")
                else:
                    linhas = []
                    for r in cfg["regras"]:
                        obrig = ", ".join(r["all"])
                        alts = ""
                        if r.get("any_groups"):
                            alts = " | alt: " + ", ".join(r["any_groups"][0])
                        linhas.append(f"• **{r['name']}**: {obrig}{alts}")
                    await event.reply(
                        "📋 **Regras ativas:**\n" + "\n".join(linhas)
                    )

            elif cmd == "/add":
                blocos = [b.strip() for b in arg.split("|")]
                if len(blocos) < 2 or not blocos[0] or not blocos[1]:
                    await event.reply(
                        "Uso: `/add Nome | obrig1, obrig2 | alt1, alt2`"
                    )
                    return
                nome = blocos[0]
                obrig = separar(blocos[1])
                alts = []
                if len(blocos) > 2 and blocos[2]:
                    alts = [separar(blocos[2])]
                cfg["regras"] = [
                    r for r in cfg["regras"]
                    if r["name"].lower() != nome.lower()
                ]
                cfg["regras"].append(
                    {"name": nome, "all": obrig, "any_groups": alts}
                )
                salvar_config(cfg)
                renderizar_regras()
                await event.reply(f"✅ Regra **{nome}** adicionada!")

            elif cmd == "/del":
                if not arg:
                    await event.reply("Uso: `/del Nome`")
                    return
                antes = len(cfg["regras"])
                cfg["regras"] = [
                    r for r in cfg["regras"]
                    if r["name"].lower() != arg.lower()
                ]
                salvar_config(cfg)
                renderizar_regras()
                if len(cfg["regras"]) < antes:
                    await event.reply(f"🗑️ Regra **{arg}** removida.")
                else:
                    await event.reply(f"Regra **{arg}** não encontrada.")

            elif cmd == "/grupos":
                if not cfg["grupos"]:
                    await event.reply(
                        "🌐 Monitorando **TODOS** os grupos e canais."
                    )
                else:
                    lista = "\n".join(f"• `{g}`" for g in cfg["grupos"])
                    await event.reply(
                        f"📌 **Grupos filtrados ({len(cfg['grupos'])}):**"
                        f"\n{lista}"
                    )

            elif cmd == "/meusgrupos":
                linhas = []
                async for d in tg_client.iter_dialogs():
                    if d.is_group or d.is_channel:
                        pid = utils.get_peer_id(d.entity)
                        uname = getattr(d.entity, "username", None)
                        user = f" (@{uname})" if uname else ""
                        linhas.append(f"• **{d.name}**{user} → `{pid}`")
                if not linhas:
                    await event.reply("Nenhum grupo ou canal encontrado.")
                else:
                    await event.reply(
                        "📂 **Seus grupos/canais:**\n"
                        + "\n".join(linhas[:50])
                    )

            elif cmd == "/addgrupo":
                if not arg:
                    await event.reply("Uso: `/addgrupo id_ou_user`")
                    return
                try:
                    alvo = int(arg) if arg.lstrip("-").isdigit() else arg
                    entidade = await tg_client.get_entity(alvo)
                    pid = utils.get_peer_id(entidade)
                    if pid not in cfg["grupos"]:
                        cfg["grupos"].append(pid)
                        salvar_config(cfg)
                    nome_ent = getattr(entidade, "title", None)
                    if not nome_ent:
                        nome_ent = getattr(entidade, "username", str(pid))
                    await event.reply(
                        f"✅ Grupo **{nome_ent}** (`{pid}`) adicionado."
                    )
                except Exception as ex:
                    await event.reply(f"❌ Não encontrei `{arg}`: {ex}")

            elif cmd == "/delgrupo":
                if not arg:
                    await event.reply("Uso: `/delgrupo id_ou_user`")
                    return
                try:
                    alvo = int(arg) if arg.lstrip("-").isdigit() else arg
                    entidade = await tg_client.get_entity(alvo)
                    pid = utils.get_peer_id(entidade)
                    alvo_ids = {pid, getattr(entidade, "id", pid)}
                except Exception:
                    alvo_ids = set()
                    if arg.lstrip("-").isdigit():
                        alvo_ids = {int(arg)}
                antes = len(cfg["grupos"])
                cfg["grupos"] = [
                    g for g in cfg["grupos"] if g not in alvo_ids
                ]
                salvar_config(cfg)
                if len(cfg["grupos"]) < antes:
                    await event.reply(f"🗑️ Grupo `{arg}` removido.")
                else:
                    await event.reply(f"Grupo `{arg}` não estava no filtro.")

            elif cmd == "/limpargrupos":
                cfg["grupos"] = []
                salvar_config(cfg)
                await event.reply(
                    "🌐 Filtro limpo! Monitorando **TODOS** os grupos."
                )

            elif cmd == "/testar":
                if not arg:
                    await event.reply("Uso: `/testar frase de exemplo`")
                    return
                batidas = checar_regras(arg, cfg["regras"])
                if batidas:
                    await event.reply(
                        f"🎯 **Bateu nas regras:** {', '.join(batidas)}"
                    )
                else:
                    await event.reply("❌ Nenhuma regra bateu.")

        async def handler_grupos(event):
            nonlocal alertas_enviados
            if event.out or event.is_private:
                return
            if cfg["grupos"]:
                cid = event.chat_id
                if cid not in cfg["grupos"]:
                    return
            texto = event.raw_text or ""
            if not texto:
                return
            batidas = checar_regras(texto, cfg["regras"])
            if not batidas:
                return

            chat = await event.get_chat()
            user = getattr(chat, "username", None)
            nome_chat = getattr(chat, "title", None) or user or "Grupo"
            link = ""
            if user:
                link = f"\n\n🔗 https://t.me/{user}/{event.id}"

            alerta = (
                "🔔 **Promoção encontrada!**\n"
                f"🏷️ **Alerta:** {', '.join(batidas)}\n"
                f"📍 **Grupo:** {nome_chat}\n\n"
                f"{texto[:550]}{link}"
            )

            notif_cfg = cfg.get("notifications", {})
            if plyer and notif_cfg.get("popup", True):
                resumo = f"{', '.join(batidas)} — {nome_chat}\n{texto[:200]}"

                def show_notif():
                    try:
                        plyer.notification.notify(
                            title="Promoção detectada!",
                            message=resumo,
                            app_name="MonitorPromocoes",
                            timeout=10,
                        )
                    except Exception:
                        pass

                await asyncio.to_thread(show_notif)

            await tg_client.send_message("me", alerta)
            alertas_enviados += 1
            txt_contador.value = (
                f"Alertas enviados nesta sessão: {alertas_enviados}"
            )
            page.update()

        ev_cmd = events.NewMessage(chats="me")
        ev_grp = events.NewMessage()
        tg_client.add_event_handler(comando_mensagens_salvas, ev_cmd)
        tg_client.add_event_handler(handler_grupos, ev_grp)
        handlers_registrados.append((comando_mensagens_salvas, ev_cmd))
        handlers_registrados.append((handler_grupos, ev_grp))

        monitorando = True
        ui_monitorando()
        txt_erro_detalhe.value = (
            "Monitor ativo! Envie /ajuda nas Mensagens Salvas."
        )
        page.update()

        listen_task = asyncio.create_task(tg_client.run_until_disconnected())

    # ---------- Handlers dos botoes ----------
    async def on_ligar_click(e):
        nonlocal client, phone_code_hash

        txt_erro_detalhe.value = ""
        api_id_str = inp_api_id.value.strip()
        api_hash = inp_api_hash.value.strip()
        tel = inp_tel.value.strip()

        if not api_id_str.isdigit() or not api_hash or len(tel) < 10:
            txt_erro_detalhe.value = (
                "Preencha API ID, API HASH e telefone com +55."
            )
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

            sessao = StringSession(cfg.get("session_string", ""))
            client = TelegramClient(sessao, cfg["api_id"], cfg["api_hash"])
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
                txt_erro_detalhe.value = (
                    "Digite o código que chegou no seu Telegram!"
                )
                page.update()
        except Exception as ex:
            txt_erro_detalhe.value = (
                f"Erro ao conectar: {ex}\n\n{traceback.format_exc()}"
            )
            txt_erro_detalhe.color = ft.colors.RED_700
            ui_desligado()
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
                await client.sign_in(
                    cfg["telefone"],
                    codigo,
                    phone_code_hash=phone_code_hash,
                )
            except SessionPasswordNeededError:
                if not senha:
                    inp_senha_2fa.visible = True
                    txt_erro_detalhe.value = (
                        "Sua conta tem senha de 2 etapas. "
                        "Digite abaixo e confirme de novo."
                    )
                    page.update()
                    return
                await client.sign_in(password=senha)

            cfg["session_string"] = client.session.save()
            salvar_config(cfg)
            await iniciar_escuta(client)
        except Exception as ex:
            txt_erro_detalhe.value = (
                f"Código ou senha inválidos: {ex}\n\n"
                f"{traceback.format_exc()}"
            )
            txt_erro_detalhe.color = ft.colors.RED_700
            ui_desligado()
            page.update()
        finally:
            btn_confirmar_codigo.disabled = False
            page.update()

    async def on_desligar_click(e):
        nonlocal monitorando

        monitorando = False
        if client:
            for cb, ev in handlers_registrados:
                client.remove_event_handler(cb, ev)
            handlers_registrados.clear()
        if listen_task and not listen_task.done():
            listen_task.cancel()
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        ui_desligado()
        txt_erro_detalhe.value = "Monitor desligado."
        txt_erro_detalhe.color = ft.colors.GREY_800
        page.update()

    btn_ligar.on_click = on_ligar_click
    btn_confirmar_codigo.on_click = on_confirmar_codigo_click
    btn_desligar.on_click = on_desligar_click

    # ---------- Tutorial ----------
    texto_tutorial = (
        "1. Acesse my.telegram.org no navegador.\n"
        "2. Entre com seu número (+55 + DDD) e o código "
        "que chegar no Telegram.\n"
        "3. Toque em 'API development tools'.\n"
        "4. Crie um app com qualquer nome (ex: Monitor) "
        "e copie App api_id e App api_hash."
    )
    tutorial_box = ft.ExpansionTile(
        title=ft.Text("❓ Como pegar API ID e API HASH?"),
        controls=[
            ft.Container(
                padding=10,
                content=ft.Text(texto_tutorial, size=13),
            )
        ],
    )

    # ---------- Abas ----------
    campos_conexao = ft.Column(
        [
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

    view_conexao = ft.Column(
        [
            ft.Container(
                padding=16,
                border_radius=12,
                bgcolor=ft.colors.BLUE_GREY_50,
                content=ft.Column([txt_status, txt_contador]),
            ),
            tutorial_box,
            campos_conexao,
            ft.Row(
                [chk_popup, drp_som, chk_vibrar],
                alignment=ft.MainAxisAlignment.CENTER,
                wrap=True,
            ),
        ],
        spacing=12,
    )

    view_regras = ft.Column(
        [
            ft.Text(
                "Criar Novo Alerta", size=18, weight=ft.FontWeight.BOLD
            ),
            inp_nome_regra,
            inp_obrig,
            inp_alt,
            btn_add_regra,
            ft.Divider(),
            ft.Text(
                "Meus Alertas Ativos", size=16, weight=ft.FontWeight.BOLD
            ),
            col_lista_regras,
        ],
        spacing=10,
    )

    view_grupos = ft.Column(
        [
            ft.Text("Escolher Grupos", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Se nenhum grupo estiver marcado, "
                "o app vigia TODOS os seus grupos.",
                size=13,
                color=ft.colors.GREY_700,
            ),
            btn_carregar_grupos,
            col_lista_grupos,
        ],
        spacing=10,
    )

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
            ft.NavigationBarDestination(
                icon=ft.icons.POWER_SETTINGS_NEW, label="Conexão"
            ),
            ft.NavigationBarDestination(
                icon=ft.icons.NOTIFICATIONS_ACTIVE, label="Regras"
            ),
            ft.NavigationBarDestination(
                icon=ft.icons.GROUPS, label="Grupos"
            ),
        ],
    )

    page.add(conteudo)


ft.app(target=main)
