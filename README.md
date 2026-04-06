---
title: Website Downloader
emoji: 🌐
colorFrom: black
colorTo: gray
sdk: docker
app_file: app.py
pinned: false
---
# 🌐 Website Downloader (CLONER_OS)

Um poderoso downloader/clonador visual capaz de realizar réplicas completas de páginas web garantindo renderização de JavaScript. A ferramenta é equipada com uma nova interface visual inspirada em terminais de Engenharia Digital (*CLONER_OS*) com logs em tempo real via Server-Sent Events (SSE).

## ✨ Funcionalidades

- 📥 **Clonagem Completa:** Download de HTML, CSS, Imagens, Fontes e SVG locais.
- 🎭 **Motor Playwright:** Renderização prévia de JavaScript para sites complexos (SPAs).
- 🖼️ **Auto-Scroller Remoto:** Rola as páginas nativamente para forçar renderização de assets "lazy-loaded" antes do download.
- 📦 **Compactador Seguro:** Entrega um arquivo `.zip` pronto para ser aberto offline.
- 👨‍💻 **Console Tempo-Real:** Acompanhamento visual estilo *MS-DOS* direto na interface.
- 🧹 **Garbage Collector:** Rotina para limpar pastas temporárias no servidor pós-download de forma inteligente.
- 🛡️ **Bypass de Bloqueios:** Soluciona falsos-bloqueios no offline (Next.js Hydration, Smooth Scroll bloqueante de tela como Lenis e framer-motion).

---

## 🚀 Deploy / Nuvem (Grátis)

O projeto usa **Docker**, sendo totalmente compatível com diversos serviços.

Recomendamos **fortemente** o **Hugging Face Spaces**. Ele garante uma máquina de até **16 GB de RAM totalmente grátis**, necessária para a estabilidade do Chromium (Playwright Browser) em nuvem.

1. Faça o Push ou Importe esta pasta para um repositório seu no **GitHub**.
2. Vá até o [Hugging Face Spaces](https://huggingface.co/spaces) e crie um **New Space**.
3. Escolha **Docker** (Blank Template).
4. Sincronize com o seu repositório recém-criado do GitHub. O deploy ocorrerá em ~3 minutos.

---

## 🛠️ Desenvolvimento Local (Windows/Mac/Linux)

Caso queira rodar localmente, utilize o Python nativo na sua máquina com um Ambiente Virtual:

### 1. Criar e ativar Ambiente Virtual 
```bash
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / Mac
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Instalar Pacotes e Motor do Navegador
```bash
# Baixa as libs nativas do script
python -m pip install flask playwright requests beautifulsoup4 urllib3

# Baixa o navegador "invisível" (Sempre necessário rodar ao menos 1 vez)
python -m playwright install chromium
```
*(Nota: O `gunicorn` se encontra no requirements.txt, mas caso você esteja utilizando o Windows pode omiti-lo das intalações manuais pelo pip, já que serve apenas em produção Linux).*

### 3. Rodar Ferramenta
```bash
python app.py
```
Acesse a nova interface de controle em: `http://localhost:5001`

---

## 📁 Arquitetura
```text
.
├── app.py              # Aplicação Server (Flask) & Endpoint Logs (SSE)
├── downloader.py       # Engine Pesado de Interceptação da Automação Web
├── Dockerfile          # Scripts Linux (Para o Hugging Face Spaces / Render)
├── entrypoint.sh       # Gatilho de boot (Gunicorn Production Web Server)
├── static/             # Assets fixos novos do front-end customizado
├── templates/          
│   └── index.html      # Nova Interface CLONER_OS e Controller JS interativo
└── requirements.txt    # Lista de dependências (Revisado)
```

## 📄 Licença
Uso Pessoal. Não deve ser acionado contra sites com restrições severas de direitos autorais sem prévia autorização.
# website-downloader

