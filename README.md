# Monitor de Promoções do Telegram

![Banner](https://raw.githubusercontent.com/cavalcanti15/Monitor-de-promo-es-Telegram/main/screenshot.png)

## Descrição
Aplicação mobile para monitorizar promoções em grupos do Telegram.  
Utiliza a API **Telethon** para ler mensagens em tempo real e o framework **Flet** para a interface gráfica.

## Funcionalidades
- ✅ Conexão com conta Telegram (API ID / API HASH).
- ✅ Leitura de mensagens de grupos e "Mensagens Salvas".
- ✅ Sistema de **regras** (palavras‑chave e grupos filtrados).
- ✅ Notificações nativas (popup, som, vibração) configuráveis.
- ✅ (Em breve) **Sincronização** de regras via Firebase entre dispositivos.
- ✅ Serviço em segundo plano com **WorkManager** para permanecer ativo.

## Como executar localmente
```bash
# 1. Clone o repositório
git clone https://github.com/cavalcanti15/Monitor-de-promo-es-Telegram.git
cd Monitor-de-promo-es-Telegram

# 2. Instale as dependências do Flutter
flutter pub get

# 3. Instale as dependências Python (necessárias para o build APK)
pip install -r requirements.txt

# 4. Execute o app
flet run main.py   # ou use o workflow do GitHub Actions para gerar o APK
```

## Como gerar o APK (via GitHub Actions)
1. Faça push para a branch `main`.  
2. Vá à aba **Actions** e execute o workflow *Build Android APK*.  
3. Quando terminar, faça download do artefato `MonitorPromocoes-APK`.  
4. O ficheiro `build/apk/app-release.apk` está pronto para ser instalado em dispositivos Android.

## Próximos passos
- Integrar **Firebase Realtime Database** para sincronizar regras entre dispositivos.
- Permitir import/export de regras via ficheiro JSON.
- Adicionar suporte a temas e ícones personalizados.

## Licença
Este projeto está licenciado sob a licença **MIT**. Veja o ficheiro `LICENSE` para mais detalhes.
