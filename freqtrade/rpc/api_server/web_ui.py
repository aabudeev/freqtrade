from pathlib import Path

from fastapi import APIRouter
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse
from starlette.responses import FileResponse


router_ui = APIRouter(include_in_schema=False, tags=["Web UI"])


@router_ui.get("/favicon.ico")
async def favicon():
    return FileResponse(str(Path(__file__).parent / "ui/favicon.ico"))


@router_ui.get("/fallback_file.html")
async def fallback():
    return FileResponse(str(Path(__file__).parent / "ui/fallback_file.html"))


@router_ui.get("/ui_version")
async def ui_version():
    from freqtrade.commands.deploy_ui import read_ui_version

    uibase = Path(__file__).parent / "ui/installed/"
    version = read_ui_version(uibase)

    return {
        "version": version if version else "not_installed",
    }


@router_ui.get("/signals_ui", response_class=HTMLResponse)
async def signals_ui():
    """
    Отдает HTML страницу дашборда для мониторинга сигналов.
    """
    html_path = Path(__file__).parent / "signals_dashboard.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "Dashboard HTML file not found."


@router_ui.get("/{rest_of_path:path}")
async def index_html(rest_of_path: str):
    """
    Emulate path fallback to index.html.
    """
    if rest_of_path.startswith("api") or rest_of_path.startswith("."):
        raise HTTPException(status_code=404, detail="Not Found")
    uibase = (Path(__file__).parent / "ui/installed/").resolve()
    filename = (uibase / rest_of_path).resolve()
    # It's security relevant to check "relative_to".
    # Without this, Directory-traversal is possible.
    media_type: str | None = None
    if filename.suffix == ".js":
        # Force text/javascript for .js files - Circumvent faulty system configuration
        media_type = "application/javascript"
    if filename.is_file() and filename.is_relative_to(uibase):
        return FileResponse(str(filename), media_type=media_type)

    index_file = uibase / "index.html"
    if not index_file.is_file():
        return FileResponse(str(uibase.parent / "fallback_file.html"))
        
    # Inject custom Signals tab script into the compiled Vue SPA
    content = index_file.read_text(encoding="utf-8")
    injection_script = """
    <script>
    (function() {
        // Наблюдатель за DOM для поиска верхнего меню (Vuetify v-toolbar)
        const observer = new MutationObserver((mutations, obs) => {
            // Ищем ссылки в меню по href="/logs" (так как она точно есть)
            const logsLink = document.querySelector('a[href="/logs"]');
            if (logsLink && !document.getElementById('custom-signals-tab')) {
                // Создаем кнопку Signals
                const btn = document.createElement('a');
                btn.id = 'custom-signals-tab';
                // Копируем классы от соседней ссылки для стилизации
                btn.className = logsLink.className;
                // Имитируем Vuetify кнопку
                btn.innerHTML = '<span class="v-btn__content">Signals</span>';
                btn.style.cursor = 'pointer';
                
                // Вставляем после Logs
                logsLink.parentNode.insertBefore(btn, logsLink.nextSibling);
                
                // Создаем iframe для дашборда
                const iframe = document.createElement('iframe');
                iframe.id = 'signals-iframe';
                iframe.src = '/signals_ui';
                iframe.style.position = 'fixed';
                iframe.style.top = '64px'; // под тулбаром
                iframe.style.left = '0';
                iframe.style.width = '100%';
                iframe.style.height = 'calc(100vh - 64px)';
                iframe.style.border = 'none';
                iframe.style.zIndex = '9999';
                iframe.style.display = 'none'; // Скрыт по умолчанию
                iframe.style.background = '#0b0f19'; // Темный фон
                document.body.appendChild(iframe);
                
                // Логика переключения
                btn.onclick = (e) => {
                    e.preventDefault();
                    iframe.style.display = 'block';
                    // Убираем активный класс у других вкладок
                    document.querySelectorAll('.v-btn--active').forEach(el => el.classList.remove('v-btn--active'));
                    btn.classList.add('v-btn--active');
                };
                
                // Скрываем iframe если кликнули по любой другой ссылке в меню
                const allLinks = document.querySelectorAll('a[href^="/"]');
                allLinks.forEach(link => {
                    if(link.id !== 'custom-signals-tab') {
                        link.addEventListener('click', () => {
                            iframe.style.display = 'none';
                            btn.classList.remove('v-btn--active');
                        });
                    }
                });
                
                obs.disconnect(); // Нашли меню, выключаем наблюдатель
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    })();
    </script>
    """
    if "</body>" in content:
        content = content.replace("</body>", injection_script + "</body>")
        return HTMLResponse(content)
        
    # Fall back to file if no </body> found
    return FileResponse(str(index_file))
