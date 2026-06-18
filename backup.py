#!/usr/bin/env python3
"""
ESPRIT Complete Downloader
- Logs to ./logs/download_TIMESTAMP.log
- Embeds images inline as base64 (fixes missing images in saved HTML)
- Diagnoses every URL in the body: tries to fetch it, reports status
- Faithful HTML reproduction (keeps all inline styles, fonts, colours)
- Fixes ultraDocumentBody naming (uses parent folder title)
"""

from blackboard import BlackBoardClient
from bs4 import BeautifulSoup
import json
import sys
import os
import re
import base64
import html as html_lib
import logging
import getpass
from datetime import datetime
from colorama import Fore, Style, init
from urllib.parse import unquote, urlparse
from typing import Optional, List
import time as time_module
from silly_logger import Logger

init()

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs('./logs', exist_ok=True)

class StripAnsi(logging.Formatter):
    _re = re.compile(r'\x1b\[[0-9;]*m')
    def format(self, record):
        return self._re.sub('', super().format(record))

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter('%(message)s'))
log = logging.getLogger('esprit')
log.setLevel(logging.DEBUG)
log.addHandler(_ch)

# log_filename and file handler are added in main() after we know the BB class label
log_filename = None  # set in main()

def _setup_log_file(label: str) -> str:
    """
    Create the per-run log file using the Blackboard class label
    (e.g. '3AI') instead of the OS username.
    Returns the path so it can be copied to recent.log later.
    """
    global log_filename
    # Remove any existing file handlers so logs don't stack across batch users
    for h in log.handlers[:]:
        if isinstance(h, logging.FileHandler):
            h.close()
            log.removeHandler(h)
    safe_label = re.sub(r'[^A-Za-z0-9_-]', '', label) or 'unknown'
    log_filename = f"./logs/download_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_label}.log"
    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setFormatter(StripAnsi('%(asctime)s  %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    log.addHandler(fh)
    return log_filename

def _get_bb_class_label(client, courses=None) -> str:
    """
    Determine the student's class label (e.g. '3IA2') for use in the log filename.

    Priority order:
      1. Extract from enrolled course names — courses are named like 'Subject__3IA2',
         so we pull the suffix after '__' which is the most reliable source.
      2. studentId / externalId field on the user profile.
      3. batch_uid (strip trailing 4-digit year, e.g. 'ESE_ST_231JFT' → keep as-is
         if no 4-digit suffix found).
      4. OS username fallback.
    """
    # Strategy 1: parse class from course name suffix (e.g. "Financial Analysis__3IA2")
    # Count occurrences of each candidate suffix and return the most common one
    if courses:
        counts = {}
        for course in courses:
            name = course.name or ''
            if '__' in name:
                suffix = name.rsplit('__', 1)[-1].strip()
                # Accept short class codes: e.g. '3IA2', '2ING1', '1INFO'
                if suffix and re.match(r'^\d[A-Za-z]{1,6}\d*$', suffix):
                    counts[suffix] = counts.get(suffix, 0) + 1
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
    # Strategy 2: user profile studentId / externalId
    try:
        resp = client.send_get_request(
            f"/learn/api/public/v1/users/{client.user_id}", silent_on_error=True)
        if resp and resp.status_code == 200:
            data = resp.json()
            student_id = data.get('studentId') or data.get('externalId') or ''
            if student_id:
                # Strip trailing 4-digit year suffix (e.g. '3AI2425' → '3AI')
                label = re.sub(r'\d{4}$', '', str(student_id)).strip()
                if label:
                    return label
    except Exception:
        pass
    # Strategy 3: batch_uid
    if client.batch_uid:
        label = re.sub(r'\d{4}$', '', str(client.batch_uid)).strip()
        if label:
            return label
    return client.username or 'unknown'
# ─────────────────────────────────────────────────────────────────────────────


# ── URL helpers ───────────────────────────────────────────────────────────────
def is_blackboard_url(url, site):
    if url.startswith('/'):
        return True
    return urlparse(url).netloc == urlparse(site).netloc

def absolute_url(url, site):
    if url.startswith('/'):
        return site.rstrip('/') + url
    return url

def parse_ultra_attempt_url(url: str) -> dict:
    """
    Parse a BB Ultra attempt review URL and return a dict of extracted IDs.
    Example URL:
      /ultra/courses/_20696_1/outline/assessment/_3025875_1/overview/attempt/_7728_1/review/...
      ?attemptId=_7728_1&columnId=_393277_1&contentId=_3025875_1&courseId=_20696_1
    Returns keys: courseId, contentId, columnId, attemptId  (any may be absent)
    """
    result = {}
    # Query-string params (most reliable)
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ('courseId', 'contentId', 'columnId', 'attemptId'):
        vals = qs.get(key)
        if vals:
            result[key] = vals[0]
    # Path segments as fallback: /courses/_X/outline/assessment/_Y/overview/attempt/_Z
    path_re = re.compile(
        r'/courses/(?P<courseId>[^/]+)/outline/assessment/(?P<contentId>[^/]+)'
        r'(?:/overview/attempt/(?P<attemptId>[^/?]+))?')
    m = path_re.search(parsed.path)
    if m:
        for key in ('courseId', 'contentId', 'attemptId'):
            if m.group(key) and key not in result:
                result[key] = m.group(key)
    return result


def fetch_as_base64(session, url, site, indent=''):
    """
    Try to GET url with the authenticated session.
    Returns (base64_data, mime_type, status_description)
    """
    full_url = absolute_url(url, site)
    try:
        r = session.get(full_url, allow_redirects=True, timeout=15)
        status = f"{r.status_code} {r.reason}"
        if r.status_code == 200:
            mime = r.headers.get('Content-Type', '').split(';')[0].strip() or 'application/octet-stream'
            b64  = base64.b64encode(r.content).decode('ascii')
            log.debug(f"{indent}  [embed] OK {r.status_code} — {full_url[:80]}")
            return b64, mime, status
        else:
            log.warning(f"{indent}  [embed] FAIL {r.status_code} — {full_url[:80]}")
            return None, None, status
    except Exception as e:
        log.error(f"{indent}  [embed] ERROR — {full_url[:80]} — {e}")
        return None, None, str(e)
# ─────────────────────────────────────────────────────────────────────────────


# ── Shared attachment-viewer widget ──────────────────────────────────────────
#    CSS and JS are inlined directly into every generated HTML page via
#    inline_viewer_tags(), so no external .css/.js files are ever written.

_VIEWER_CSS = """
.av-doc-viewer{
  --av-bar-bg:#1c1c1c;--av-bar-fg:#f2f2f2;--av-bar-fg-dim:#b8b8b8;
  --av-page-bg:#f4f5f6;--av-card-bg:#fff;--av-accent:#3b6fe0;
  --av-border:#dcdde0;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  color:#1f1f1f;max-width:960px;margin:16px 0;position:relative;
  border:1px solid var(--av-border);border-radius:8px;
  background:var(--av-card-bg);
  box-shadow:0 1px 3px rgba(0,0,0,.08);
}
/* NOTE: overflow:hidden was removed from .av-doc-viewer above — it was
   clipping the absolutely-positioned "Copy file path" dropdown, making it
   invisible (especially when the card is collapsed, since the container's
   height was just the header). Corner-rounding is now done locally on the
   header and content wrap instead (see below), so the rounded-card look is
   preserved without clipping the dropdown. */
.av-doc-viewer,.av-doc-viewer *{box-sizing:border-box;margin:0;padding:0;}
.av-doc-header{
  display:flex;align-items:center;gap:10px;
  padding:10px 14px;border-bottom:1px solid var(--av-border);
  background:var(--av-card-bg);cursor:pointer;user-select:none;
  border-radius:8px;
}
.av-doc-viewer.av-static .av-doc-header{cursor:default;}
.av-file-icon-wrap{
  width:28px;height:28px;flex:none;border-radius:5px;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:700;letter-spacing:-.3px;
  background:#e8edf4;color:#3b6fe0;
}
.av-file-name{
  flex:1;font-size:14px;font-weight:600;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.av-doc-header button{
  background:none;border:none;cursor:pointer;color:#5b6b7c;
  padding:6px;border-radius:4px;font-size:15px;line-height:1;
}
.av-doc-header button:hover{background:#f0f1f2;}
.av-menu-wrap{position:relative;}
.av-dots-btn{
  background:none;border:none;color:#5b6b7c;
  border-radius:4px;padding:6px;font-size:15px;
  cursor:pointer;letter-spacing:1px;line-height:1;
}
.av-dots-btn:hover{background:#f0f1f2;}
.av-dropdown-menu{
  position:absolute;right:0;top:calc(100% + 4px);
  background:#fff;border:1px solid var(--av-border);
  border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.16);
  min-width:160px;z-index:1000;overflow:hidden;display:none;
}
.av-dropdown-menu.av-open{display:block;}
.av-dropdown-menu button{
  display:flex;align-items:center;gap:8px;width:100%;
  background:none;border:none;color:#1f1f1f;padding:9px 14px;
  font-size:13px;cursor:pointer;text-align:left;
}
.av-dropdown-menu button:hover{background:#f5f6f7;}
.av-dropdown-menu button svg{flex:none;}
.av-content-wrap{display:none;overflow:hidden;border-radius:0 0 8px 8px;}
.av-content-wrap.av-open{display:block;}
/* Custom lightweight toolbar (replaces the raw <iframe src=pdf> approach,
   which surfaced the browser's heavy native PDF toolbar). Centered via a
   3-column grid so the page/zoom controls sit in the middle regardless of
   the fullscreen button's width on the right. */
.av-toolbar{
  display:none;grid-template-columns:1fr auto 1fr;align-items:center;
  background:var(--av-bar-bg);color:var(--av-bar-fg);
  padding:7px 14px;font-size:13px;
}
.av-toolbar.av-open{display:grid;}
.av-tb-center{display:flex;align-items:center;justify-content:center;gap:4px;}
.av-tb-right{display:flex;justify-content:flex-end;}
.av-toolbar button{
  background:none;border:none;color:var(--av-bar-fg);
  cursor:pointer;padding:5px 8px;border-radius:4px;font-size:14px;line-height:1;
}
.av-toolbar button:hover{background:rgba(255,255,255,.12);}
.av-toolbar button:disabled{color:#555;cursor:default;}
.av-toolbar button:disabled:hover{background:none;}
.av-page-indicator{color:var(--av-bar-fg-dim);display:inline-flex;align-items:center;gap:5px;margin:0 2px;}
.av-page-indicator input{
  width:30px;text-align:center;background:var(--av-accent);color:#fff;
  border:none;border-radius:4px;padding:3px 2px;font-size:13px;font-weight:600;
}
.av-tb-divider{width:1px;height:16px;background:rgba(255,255,255,.15);display:inline-block;margin:0 4px;}
.av-zoom-level{color:var(--av-bar-fg-dim);min-width:38px;text-align:center;display:inline-block;}
.av-canvas-area{
  width:100%;height:680px;background:#f4f5f6;
  overflow:auto;display:flex;flex-direction:column;align-items:center;
  padding:12px 0;gap:8px;
}
.av-canvas-area canvas{display:block;box-shadow:0 2px 8px rgba(0,0,0,.18);}
.av-pdf-msg{color:#888;font-size:13px;padding:40px;text-align:center;}
"""

_VIEWER_JS = r"""
(function(){
  'use strict';
  function escHtml(s){
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function mount(el){
    if(el.dataset.avInit) return;
    el.dataset.avInit='1';
    el.classList.add('av-doc-viewer');

    var relFile = el.dataset.file||'';
    var name    = el.dataset.name || relFile.split('/').pop() || relFile;
    var ext     = (name.split('.').pop()||'').toLowerCase();
    var isPdf   = ext==='pdf';
    var absPath = el.dataset.abs||'';
    var b64     = el.dataset.b64||'';   // base64 PDF bytes embedded at generation time

    var extLabel  = name.split('.').pop().toUpperCase().slice(0,4)||'FILE';
    var iconColor = isPdf ? '#e74c3c' : '#3b6fe0';
    var iconBg    = isPdf ? '#fdecea' : '#e8edf4';

    el.innerHTML =
      '<div class="av-doc-header">' +
        '<div class="av-file-icon-wrap" style="color:'+iconColor+';background:'+iconBg+'">'+extLabel+'</div>' +
        '<span class="av-file-name">'+escHtml(name)+'</span>' +
        '<div class="av-menu-wrap">' +
          '<button class="av-dots-btn" title="More options">•••</button>' +
          '<div class="av-dropdown-menu">' +
            '<button class="av-copy-path">' +
              '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>' +
              '<span class="av-copy-label">Copy file path</span>' +
            '</button>' +
          '</div>' +
        '</div>' +
        (isPdf ? '<button class="av-collapse-btn" title="Expand">⌄</button>' : '') +
      '</div>' +
      (isPdf ?
        '<div class="av-toolbar">' +
          '<div class="av-tb-left"></div>' +
          '<div class="av-tb-center">' +
            '<button class="av-zoom-out" title="Zoom out">−</button>' +
            '<span class="av-zoom-level">100%</span>' +
            '<button class="av-zoom-in" title="Zoom in">+</button>' +
          '</div>' +
          '<div class="av-tb-right"><button class="av-fullscreen" title="Fullscreen">⤢</button></div>' +
        '</div>' +
        '<div class="av-content-wrap"><div class="av-canvas-area"></div></div>'
      : '');

    if(!isPdf){ el.classList.add('av-static'); }

    var header      = el.querySelector('.av-doc-header');
    var contentWrap = el.querySelector('.av-content-wrap');
    var collapseBtn = el.querySelector('.av-collapse-btn');
    var dotsBtn     = el.querySelector('.av-dots-btn');
    var dropdown    = el.querySelector('.av-dropdown-menu');
    var copyBtn     = el.querySelector('.av-copy-path');
    var copyLabel   = el.querySelector('.av-copy-label');

    if(isPdf){
      var toolbar    = el.querySelector('.av-toolbar');
      var canvasArea = el.querySelector('.av-canvas-area');
      var zoomOutBtn = el.querySelector('.av-zoom-out');
      var zoomInBtn  = el.querySelector('.av-zoom-in');
      var zoomLevel  = el.querySelector('.av-zoom-level');
      var fsBtn      = el.querySelector('.av-fullscreen');

      var pdfDoc  = null;
      var zoom    = 1.0;
      var loaded  = false;

      function renderAll(){
        if(!pdfDoc) return;
        canvasArea.innerHTML = '';
        var total = pdfDoc.numPages;
        for(var i=1; i<=total; i++){
          (function(pageNum){
            pdfDoc.getPage(pageNum).then(function(page){
              var vp = page.getViewport({scale: zoom});
              var canvas = document.createElement('canvas');
              canvas.width  = vp.width;
              canvas.height = vp.height;
              canvasArea.appendChild(canvas);
              page.render({canvasContext: canvas.getContext('2d'), viewport: vp});
            });
          })(i);
        }
        zoomLevel.textContent = Math.round(zoom*100)+'%';
      }

      function loadPdf(){
        if(loaded) return;
        loaded = true;
        var lib = window.pdfjsLib;
        if(!lib){
          canvasArea.innerHTML = '<div class="av-pdf-msg">pdf.js not loaded — check internet connection.</div>';
          return;
        }
        if(!b64){
          canvasArea.innerHTML = '<div class="av-pdf-msg">PDF not embedded (fetch failed during archival).</div>';
          return;
        }
        // Convert base64 → Uint8Array and hand straight to pdf.js — no fetch, no file picker
        var binary = atob(b64);
        var bytes  = new Uint8Array(binary.length);
        for(var i=0; i<binary.length; i++) bytes[i] = binary.charCodeAt(i);
        lib.getDocument({data: bytes}).promise.then(function(doc){
          pdfDoc = doc;
          renderAll();
        }).catch(function(err){
          canvasArea.innerHTML = '<div class="av-pdf-msg">Could not parse PDF: '+escHtml(String(err))+'</div>';
        });
      }

      header.addEventListener('click', function(e){
        if(e.target.closest('.av-menu-wrap')) return;
        var open = contentWrap.classList.toggle('av-open');
        toolbar.classList.toggle('av-open', open);
        collapseBtn.textContent = open ? '⌃' : '⌄';
        collapseBtn.title = open ? 'Collapse' : 'Expand';
        if(open) loadPdf();
      });

      zoomInBtn.addEventListener('click', function(){
        zoom = Math.min(+(zoom+0.25).toFixed(2), 4);
        renderAll();
      });
      zoomOutBtn.addEventListener('click', function(){
        zoom = Math.max(+(zoom-0.25).toFixed(2), 0.3);
        renderAll();
      });
      fsBtn.addEventListener('click', function(){
        if(!document.fullscreenElement){ if(canvasArea.requestFullscreen) canvasArea.requestFullscreen(); }
        else if(document.exitFullscreen){ document.exitFullscreen(); }
      });
      document.addEventListener('fullscreenchange', function(){
        fsBtn.textContent = document.fullscreenElement===canvasArea ? '⤡' : '⤢';
        canvasArea.style.height = document.fullscreenElement===canvasArea ? '100vh' : '';
      });
    }

    dotsBtn.addEventListener('click', function(e){
      e.stopPropagation();
      dropdown.classList.toggle('av-open');
    });
    document.addEventListener('click', function(){
      dropdown.classList.remove('av-open');
    });

    copyBtn.addEventListener('click', function(e){
      e.stopPropagation();
      var text = absPath || relFile;
      var reset = function(lbl){
        copyLabel.textContent = lbl;
        setTimeout(function(){ copyLabel.textContent='Copy file path'; }, 1400);
      };
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(
          function(){ reset('Copied!'); },
          function(){ reset('Copy failed'); }
        );
      } else { reset('Copy unsupported'); }
    });
  }

  function scan(){
    document.querySelectorAll('.av-mount').forEach(mount);
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', scan);
  } else { scan(); }
  window.AttachmentViewer={mount:mount,scan:scan};
})();
"""

def inline_viewer_tags():
    """Return <style>/<script> tags with the viewer CSS/JS inlined.
    pdf.js is loaded once per page from cdnjs (async, non-blocking).
    The PDF bytes themselves are embedded as base64 in data-b64 at
    generation time, so no file loading or CORS issues at view time."""
    PDFJS_URL    = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js'
    PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js'
    pdfjs_tag = (
        f'    <script src="{PDFJS_URL}" '
        f'onload="(window.pdfjsLib||window[\'pdfjs-dist/build/pdf\']||pdfjsLib)'
        f'.GlobalWorkerOptions.workerSrc=\'{PDFJS_WORKER}\'">'
        f'</script>\n'
    )
    return (
        '    <style>\n' + _VIEWER_CSS + '\n    </style>\n'
        + pdfjs_tag
        + '    <script>\n' + _VIEWER_JS + '\n    </script>\n'
    )


def inject_abs_paths(html_str, page_dir):
    """Add data-abs="<absolute OS path>" to every .av-mount div so the JS
    copy-path button can copy the real on-disk path regardless of nesting."""
    import re as _re
    def _add_abs(m):
        tag = m.group(0)
        rel = _re.search(r'data-file="([^"]*)"', tag)
        if not rel:
            return tag
        abs_path = os.path.abspath(os.path.join(page_dir, rel.group(1)))
        # Replace backslashes (Windows) with forward slashes for readability
        abs_path = abs_path.replace(os.sep, '/')
        return tag[:-1] + f' data-abs="{abs_path}">'
    return _re.sub(r'<div[^>]*class="av-mount"[^>]*>', _add_abs, html_str)


# ── Body post-processor ───────────────────────────────────────────────────────
def process_body(body_html, session, site, indent='', attachments_subdir=''):
    """
    Walk every element in the BB body and:
      1. data-bbfile with image mimeType or render=inlineOnly -> embed as <img base64>
      2. <img src="..."> pointing to BB                       -> embed src as base64
      3. All other <a href> on BB                             -> make absolute
      4. Non-image data-bbfile attachments: <a> tag is replaced entirely by
         the viewer card (no dangling BB link). PDFs get their bytes embedded
         as base64 in data-b64 for zero-dependency offline rendering.
    Returns processed HTML string.
    """
    soup = BeautifulSoup(body_html, 'html.parser')

    # ── 1. data-bbfile inline attachments ────────────────────────────────────
    for tag in soup.find_all(attrs={'data-bbfile': True}):
        raw = tag.get('data-bbfile', '')
        try:
            meta = json.loads(html_lib.unescape(raw))
        except Exception:
            continue

        mime     = meta.get('mimeType', '')
        render   = meta.get('render', '')
        fname    = meta.get('fileName', '') or meta.get('linkName', '')
        resource = meta.get('resourceUrl', '')
        href     = tag.get('href', '')

        is_image = mime.startswith('image/')

        if is_image:
            embedded = False
            for try_url in [u for u in [resource, href] if u]:
                b64, fetched_mime, status = fetch_as_base64(session, try_url, site, indent)
                if b64:
                    actual_mime = fetched_mime or mime or 'image/png'
                    img_tag = soup.new_tag(
                        'img',
                        src=f"data:{actual_mime};base64,{b64}",
                        alt=meta.get('alternativeText', fname),
                        style="max-width:100%;height:auto;display:block;margin:8px 0;")
                    tag.replace_with(img_tag)
                    log.info(f"{indent}  {Fore.GREEN}[img embedded] {fname}{Style.RESET_ALL}")
                    embedded = True
                    break
            if not embedded:
                log.warning(f"{indent}  {Fore.YELLOW}[img FAILED — kept as link] {fname}{Style.RESET_ALL}")
                if href:
                    tag['href'] = absolute_url(href, site)
        else:
            log.debug(f"{indent}  [non-image attachment] {fname} ({mime})")
            if fname:
                rel_name = sanitize_filename(fname)
                rel_path = f"{attachments_subdir}/{rel_name}" if attachments_subdir else rel_name
                is_pdf_attach = fname.lower().endswith('.pdf')
                mount_div = soup.new_tag('div')
                mount_div['class'] = 'av-mount'
                mount_div['data-file'] = rel_path
                mount_div['data-name'] = fname
                if is_pdf_attach:
                    for try_url in [u for u in [resource, href] if u]:
                        b64, _, _ = fetch_as_base64(session, try_url, site, indent)
                        if b64:
                            mount_div['data-b64'] = b64
                            log.info(f"{indent}  {Fore.GREEN}[pdf embedded] {fname}{Style.RESET_ALL}")
                            break
                    else:
                        log.warning(f"{indent}  {Fore.YELLOW}[pdf embed FAILED — viewer will be empty] {fname}{Style.RESET_ALL}")
                # Replace the <a> tag entirely with the viewer card —
                # the BB link is useless offline and the card shows the file name anyway.
                tag.replace_with(mount_div)
            else:
                # No filename — can't build a viewer; make the href absolute as fallback
                if href and not href.startswith('http'):
                    tag['href'] = absolute_url(href, site)

    # ── 2. Plain <img> tags ───────────────────────────────────────────────────
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if not src or src.startswith('data:'):
            continue
        if is_blackboard_url(src, site):
            b64, fetched_mime, status = fetch_as_base64(session, src, site, indent)
            if b64:
                img['src'] = f"data:{fetched_mime};base64,{b64}"
                log.info(f"{indent}  {Fore.GREEN}[img embedded] {src[:60]}{Style.RESET_ALL}")
            else:
                log.warning(f"{indent}  {Fore.YELLOW}[img FAILED] {src[:60]} -> {status}{Style.RESET_ALL}")
        # External images: leave alone — they'll load if internet is available

    # ── 3. Make all BB hrefs absolute ────────────────────────────────────────
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith(('data:', '#', 'mailto:')):
            continue
        if is_blackboard_url(href, site):
            a['href'] = absolute_url(href, site)

    return str(soup)
# ─────────────────────────────────────────────────────────────────────────────


# ── File helpers ─────────────────────────────────────────────────────────────

# Image types are embedded in HTML — skip them as standalone downloads
IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'tiff', 'tif'}

def extract_file_links_from_body(body_html):
    """Extract all non-image embedded file links from BB body HTML."""
    file_links = []
    if not body_html:
        return file_links

    body_str = str(body_html)

    # Method 1: data-bbfile JSON — explicit filename, any non-image type
    for match in re.findall(r'data-bbfile="({[^"]+})"', body_str):
        try:
            meta  = json.loads(html_lib.unescape(match))
            fname = meta.get('fileName') or meta.get('linkName', '')
            if not fname:
                continue
            ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
            if ext in IMAGE_EXTS:
                continue  # images are embedded in HTML, not downloaded separately

            # resourceUrl is a pre-signed /sessions/ link (may expire → 403)
            # href is the stable bbcswebdav token URL — use as primary, resourceUrl as fallback
            resource_url = meta.get('resourceUrl', '')

            # Find href from the anchor tag (href can appear before OR after data-bbfile)
            anchor_m = re.search(
                rf'<a\s[^>]*data-bbfile="{re.escape(match)}"[^>]*>', body_str)
            if not anchor_m:
                anchor_m = re.search(
                    rf'<a\s[^>]*href="[^"]*"[^>]*data-bbfile="{re.escape(match)}"[^>]*>',
                    body_str)
            href = ''
            if anchor_m:
                hm = re.search(r'href="([^"]+)"', anchor_m.group(0))
                if hm:
                    href = html_lib.unescape(hm.group(1))

            # Prefer href (stable token URL); fall back to resourceUrl
            url = href if href else resource_url
            fallback_url = resource_url if href else ''

            if url and not any(l['url'] == url for l in file_links):
                file_links.append({'url': url, 'fallback_url': fallback_url, 'filename': fname,
                                    'mime': meta.get('mimeType', '')})
        except Exception:
            pass

    return file_links


def sanitize_filename(filename):
    """
    Strip filesystem-unsafe characters the same way download_file() does,
    so anything that wants to *reference* a downloaded attachment (e.g. the
    attachment-viewer mount divs in process_body) always computes the exact
    same on-disk name that download_file() actually wrote.
    """
    return unquote(re.sub(r'[<>:"/\\|?*]', '', filename))


def download_file(client, url, filename, save_path, fallback_url=''):
    """Download any non-image file. Tries url first, then fallback_url on 403."""
    if url.startswith('/'):
        url = client.site + url
    filename_safe = sanitize_filename(filename)
    dest = os.path.abspath(os.path.join(save_path, filename_safe))
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.isfile(dest):
        log.info(f"      {Fore.YELLOW}⚠ Exists: {filename_safe}{Style.RESET_ALL}")
        return False

    urls_to_try = [u for u in [url, fallback_url] if u]
    for try_url in urls_to_try:
        if try_url.startswith('/'):
            try_url = client.site + try_url
        try:
            r = client.session.get(try_url, allow_redirects=True)
            if r.status_code == 200:
                with open(dest, 'wb') as f:
                    f.write(r.content)
                log.info(f"      {Fore.GREEN}✓ Downloaded: {filename_safe}{Style.RESET_ALL}")
                return True
            else:
                log.debug(f"      {Fore.YELLOW}⚠ {r.status_code} on {try_url[:70]}{Style.RESET_ALL}")
        except Exception as e:
            log.debug(f"      {Fore.RED}✗ Error on {try_url[:70]}: {e}{Style.RESET_ALL}")

    log.warning(f"      {Fore.RED}✗ Failed: {filename_safe}{Style.RESET_ALL}")
    return False
# ─────────────────────────────────────────────────────────────────────────────


# ── HTML page saver ───────────────────────────────────────────────────────────
def save_html_page(content, save_path, client, parent_title=None):
    if not content.body:
        return False

    raw_title = (content.title or '').strip()
    if raw_title.lower() in ('', 'ultradocumentbody') and parent_title:
        display_title = parent_title
    else:
        display_title = raw_title

    filename_base = re.sub(r'[<>:"/\\|?*]', '', display_title).strip() or 'page'
    filename      = filename_base + '.html'
    dest          = os.path.abspath(os.path.join(save_path, filename))
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # ── Always record into _order.txt (even if file already exists) ──────────
    # Must happen before the early-return so re-runs don't leave manifests
    # incomplete, which would cause pages to vanish from the section index.
    order_file = os.path.join(save_path, '_order.txt')
    try:
        existing_order = []
        if os.path.isfile(order_file):
            with open(order_file, encoding='utf-8') as _of:
                existing_order = [l.strip() for l in _of if l.strip()]
        if filename not in existing_order:
            with open(order_file, 'a', encoding='utf-8') as _of:
                _of.write(filename + '\n')
    except Exception:
        pass

    if os.path.isfile(dest):
        log.info(f"      {Fore.YELLOW}⚠ Exists: {filename}{Style.RESET_ALL}")
        return False

    log.info(f"      {Fore.CYAN}Processing HTML: {filename}{Style.RESET_ALL}")

    processed_body = process_body(content.body, client.session, client.site, indent='      ')
    processed_body = inject_abs_paths(processed_body, save_path)

    viewer_tags = inline_viewer_tags()

    # Derive the chapter name from the save_path (immediate parent folder name)
    chapter_name = html_lib.escape(os.path.basename(os.path.dirname(dest)) or '')

    html_doc = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html_lib.escape(display_title)}</title>
    <!-- Attachment viewer: inline PDF preview / local-path fallback -->
{viewer_tags}    <!-- MathJax: renders $...$ and $$...$$ LaTeX from BB content -->
    <script>
    window.MathJax = {{
        tex: {{
            inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
            displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
            processEscapes: true
        }},
        options: {{ skipHtmlTags: ['script','noscript','style','textarea','pre'] }}
    }};
    </script>
    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js" async></script>
    <style>
        *, *::before, *::after {{ box-sizing: border-box; }}

        body {{
            font-family: Arial, Helvetica, sans-serif;
            font-size: 14px;
            line-height: 1.6;
            color: #222;
            background: #fff;
            margin: 0;
            padding: 0;
        }}

        /* ━━━ BB Ultra top chrome ━━━ */
        .bb-topbar {{
            position: sticky;
            top: 0;
            z-index: 200;
            display: flex;
            align-items: center;
            justify-content: space-between;
            height: 40px;
            padding: 0 16px;
            background: #fff;
            border-bottom: 1px solid #d9dde3;
            font-size: 0.82rem;
            color: #444;
            gap: 12px;
            user-select: none;
        }}
        .bb-topbar-left {{
            display: flex;
            align-items: center;
            gap: 6px;
            min-width: 0;
            flex-shrink: 0;
        }}
        .bb-topbar-left a {{
            color: #444;
            text-decoration: none;
            white-space: nowrap;
            font-weight: 500;
        }}
        .bb-topbar-left a:hover {{ color: #1a6fb5; text-decoration: underline; }}
        .bb-topbar-sep {{
            color: #aaa;
            font-size: 0.75rem;
        }}
        .bb-topbar-chapter {{
            color: #666;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .bb-topbar-right {{
            flex-shrink: 0;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .bb-nav-pos {{
            font-size: 0.78rem;
            color: #999;
            min-width: 38px;
            text-align: center;
        }}
        .bb-topbar-right a,
        .bb-topbar-right span.bb-nav-disabled {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 4px 14px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 0.82rem;
            color: #444;
            text-decoration: none;
            background: #fff;
            cursor: pointer;
            white-space: nowrap;
            transition: background 0.12s;
        }}
        .bb-topbar-right a:hover {{ background: #f0f2f5; color: #1a1a1a; border-color: #999; }}
        .bb-topbar-right span.bb-nav-disabled {{ color: #bbb; border-color: #e0e0e0; cursor: default; }}

        /* close × button (top-left corner, like BB Ultra) */
        .bb-close {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px;
            height: 28px;
            border-radius: 4px;
            background: #e53935;
            color: #fff;
            font-size: 1rem;
            font-weight: bold;
            text-decoration: none;
            flex-shrink: 0;
            line-height: 1;
        }}
        .bb-close:hover {{ background: #c62828; }}

        /* ━━━ Page header (title under topbar) ━━━ */
        .bb-page-header {{
            padding: 28px 40px 18px 40px;
            border-bottom: 1px solid #e0e3e8;
            margin-bottom: 28px;
        }}
        .bb-page-header h1 {{
            font-size: 1.55rem;
            font-weight: 600;
            color: #1a1a1a;
            margin: 0;
        }}

        /* ━━━ Content column ━━━ */
        .bb-content {{
            max-width: 820px;
            padding: 0 40px 80px 40px;
        }}


        /* ━━━ Typography ━━━ */
        h1 {{ font-size: 1.4rem; }}
        h2 {{ font-size: 1.2rem; }}
        h3 {{ font-size: 1.05rem; }}
        h4 {{ font-size: 1rem; }}
        h5 {{ font-size: 0.95rem; }}
        h6 {{ font-size: 0.875rem; font-weight: normal; }}
        h1,h2,h3,h4,h5,h6 {{ margin-top: 1em; margin-bottom: 0.3em; }}

        img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 12px 0;
        }}

        a {{ color: #1a6fb5; text-decoration: underline; }}

        table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.9rem; }}
        th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
        th {{ background: #f4f4f4; font-weight: 600; }}

        pre, code {{ font-family: "Courier New", monospace; font-size: 0.85rem;
                     background: #f5f5f5; padding: 2px 5px; border-radius: 3px; }}
        pre {{ padding: 12px; overflow-x: auto; line-height: 1.4; }}

        blockquote {{ border-left: 4px solid #c7cdd4; margin: 0.5em 0;
                      padding: 0.3em 1em; color: #555; }}

        ul, ol {{ padding-left: 1.5em; margin: 0.5em 0; }}
        li {{ margin-bottom: 0.2em; }}

        [data-bbid] {{ display: block; }}
        p {{ margin: 0.4em 0; }}
    </style>
</head>
<body>

    <!-- BB Ultra top chrome — nav links filled in by inject_nav_links() -->
    <header class="bb-topbar">
        <div class="bb-topbar-left">
            <a href="../" class="bb-close" id="bb-nav-close" title="Sommaire">&#x2715;</a>
            <a href="../" id="bb-nav-sommaire">Sommaire</a>
            <svg class="bb-topbar-sep" width="14" height="14" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 2h8v12H4z" fill="none"/><path d="M6 2l4 6-4 6" stroke="#aaa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
            <span class="bb-topbar-chapter">{chapter_name}</span>
        </div>
        <div class="bb-topbar-right" id="bb-nav-top-right">
            <!-- filled by inject_nav_links -->
            <span class="bb-nav-disabled" id="bb-nav-prev">&#8592;&nbsp;Précédent</span>
            <span class="bb-nav-pos" id="bb-nav-position"></span>
            <span class="bb-nav-disabled" id="bb-nav-next">Suivant&nbsp;&#8594;</span>
        </div>
    </header>

    <div class="bb-page-header">
        <h1>{html_lib.escape(display_title)}</h1>
    </div>

    <div class="bb-content">
        {processed_body}
    </div>


</body>
</html>"""

    try:
        with open(dest, 'w', encoding='utf-8') as f:
            f.write(html_doc)
        log.info(f"      {Fore.GREEN}✓ Saved: {filename}{Style.RESET_ALL}")
        return True
    except Exception as e:
        log.error(f"      {Fore.RED}✗ Error saving {filename}: {e}{Style.RESET_ALL}")
        return False
# ─────────────────────────────────────────────────────────────────────────────


# ── Navigation link injector ─────────────────────────────────────────────────

def _first_html_in_dir(dirpath: str) -> Optional[str]:
    """
    Return the absolute path of the first BB-saved HTML page inside dirpath,
    following _order.txt if present, else alphabetical fallback.
    Recurses into subdirectories in order when a line in _order.txt is a folder.
    Returns None if nothing is found.
    """
    order_path = os.path.join(dirpath, '_order.txt')
    if os.path.isfile(order_path):
        try:
            with open(order_path, encoding='utf-8') as f:
                entries = [l.strip() for l in f if l.strip()]
        except Exception:
            entries = []
    else:
        entries = sorted(os.listdir(dirpath)) if os.path.isdir(dirpath) else []

    for entry in entries:
        full = os.path.join(dirpath, entry)
        if entry.lower().endswith('.html') and os.path.isfile(full):
            return full
        if os.path.isdir(full):
            result = _first_html_in_dir(full)
            if result:
                return result
    return None


def _collect_ordered_pages(dirpath: str) -> List[str]:
    """
    Return an ordered flat list of absolute paths of all BB-saved HTML pages
    under dirpath, following _order.txt manifests at every level.
    Folder entries in _order.txt are expanded recursively in order.
    """
    order_path = os.path.join(dirpath, '_order.txt')
    if os.path.isfile(order_path):
        try:
            with open(order_path, encoding='utf-8') as f:
                entries = [l.strip() for l in f if l.strip()]
        except Exception:
            entries = []
    else:
        # Fallback: html files alphabetically, then subdirs alphabetically
        entries = sorted(os.listdir(dirpath)) if os.path.isdir(dirpath) else []

    pages = []
    seen = set()
    for entry in entries:
        full = os.path.join(dirpath, entry)
        if entry.lower().endswith('.html') and os.path.isfile(full):
            if full not in seen:
                seen.add(full)
                pages.append(full)
        elif os.path.isdir(full):
            for p in _collect_ordered_pages(full):
                if p not in seen:
                    seen.add(p)
                    pages.append(p)
    return pages


_INDEX_CSS = """*,*::before,*::after{box-sizing:border-box;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
     font-size:14px;line-height:1.6;color:#1a1a1a;background:#f4f6f9;margin:0;padding:0;}
.idx-topbar{
    position:sticky;top:0;z-index:200;
    display:flex;align-items:center;justify-content:space-between;
    height:44px;padding:0 20px;
    background:#fff;border-bottom:1px solid #dde1e7;
    font-size:0.82rem;color:#555;user-select:none;
    box-shadow:0 1px 3px rgba(0,0,0,.06);
}
.idx-topbar a{color:#1a6fb5;text-decoration:none;font-weight:600;font-size:0.82rem;}
.idx-topbar a:hover{text-decoration:underline;}
.idx-topbar-center{color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    font-size:0.8rem;}
.idx-header{
    padding:36px 48px 24px 48px;
    background:#fff;border-bottom:1px solid #e0e3e8;
}
.idx-header h1{font-size:1.5rem;font-weight:700;color:#111;margin:0 0 4px 0;
    letter-spacing:-.01em;}
.idx-header p{color:#999;font-size:0.83rem;margin:0;font-weight:500;}
.idx-list{list-style:none;margin:24px 48px 48px 48px;padding:0;
    background:#fff;border-radius:10px;
    box-shadow:0 1px 4px rgba(0,0,0,.07),0 0 0 1px rgba(0,0,0,.04);}
.idx-item{
    display:flex;align-items:center;gap:14px;
    padding:13px 18px;
    border-bottom:1px solid #f0f2f5;
    transition:background 0.1s;
}
.idx-item:last-child{border-bottom:none;}
.idx-item:hover{background:#f7f9fc;}
.idx-num{
    min-width:26px;height:26px;border-radius:6px;
    background:#eef1f6;color:#666;font-size:0.75rem;
    display:flex;align-items:center;justify-content:center;
    font-weight:700;flex-shrink:0;
}
.idx-item a{color:#1a1a1a;text-decoration:none;font-size:0.9rem;font-weight:500;flex:1;
    min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.idx-item a:hover{color:#1a6fb5;}
.idx-badge{
    font-size:0.73rem;color:#fff;white-space:nowrap;flex-shrink:0;
    background:#b0bac8;border-radius:10px;padding:2px 8px;font-weight:600;
}
.idx-assignment .idx-num{background:#e8eeff;color:#3a5ec0;}
.idx-assignment:hover{background:#f3f5ff;}
.idx-assignment .idx-badge{background:#7b93d6;}
.idx-subsection .idx-num{background:#e4f2e8;color:#2d7a45;font-size:0.7rem;}
.idx-subsection:hover{background:#f2fbf5;}
.idx-subsection a{color:#1a5f34;font-weight:600;}
.idx-subsection .idx-badge{background:#5dab74;}
"""


def _write_index_html(index_path: str, title: str, subtitle: str,
                      items_html: str, retour_href: Optional[str]) -> None:
    """
    Write a BB Ultra-style index.html.
    retour_href: relative path to parent index, or None to hide the Retour link.
    """
    retour_html = (f'<a href="{html_lib.escape(retour_href)}">&#8592;&nbsp; Retour</a>'
                   if retour_href else '<span></span>')
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>{_INDEX_CSS}</style>
</head>
<body>
<div class="idx-topbar">
    {retour_html}
    <span class="idx-topbar-center">{html_lib.escape(title)}</span>
    <span></span>
</div>
<div class="idx-header">
    <h1>{html_lib.escape(title)}</h1>
    <p>{html_lib.escape(subtitle)}</p>
</div>
<ul class="idx-list">
{items_html}</ul>
</body>
</html>"""
    try:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception as e:
        log.warning(f"[nav] Could not write {index_path}: {e}")


def _generate_section_index(section_path: str, section_name: str,
                            pages: List[str], index_path: str,
                            parent_index_path: Optional[str] = None) -> None:
    """
    Generate a BB Ultra-style index.html listing all content in a section:
    - Regular pages (name/name.html) with sibling attachment badge
    - Assignment folders (contain instructions.html + attachments/) shown
      with their attachment count so they're not invisible
    Follows _order.txt at the section level for ordering.
    Rebuilds every call so re-runs stay accurate.
    """
    _ATTACH_EXTS = {'.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
                    '.zip', '.rar', '.7z', '.txt', '.csv', '.ipynb', '.py',
                    '.mp4', '.mp3', '.avi', '.mkv'}

    def _count_attachments_dir(d: str) -> int:
        """Count downloadable files recursively under d."""
        return sum(
            1 for dp, _, fns in os.walk(d)
            for fn in fns
            if not fn.startswith('_')
            and os.path.splitext(fn)[1].lower() in _ATTACH_EXTS
        )

    # Build a set of page paths for quick lookup
    page_set = set(pages)

    # Collect all entries at section level in _order.txt order
    order_path = os.path.join(section_path, '_order.txt')
    try:
        with open(order_path, encoding='utf-8') as f:
            ordered_entries = [l.strip() for l in f if l.strip()]
    except Exception:
        ordered_entries = sorted(os.listdir(section_path)) if os.path.isdir(section_path) else []

    # Also pick up assignment folders on disk not in _order.txt.
    # download_assignment() never writes to _order.txt (it's a leaf node),
    # so detect them by the presence of instructions.html inside.
    ordered_set = set(ordered_entries)
    try:
        for entry in sorted(os.listdir(section_path)):
            if entry in ordered_set or entry.startswith('_') or entry.startswith('.'):
                continue
            ep = os.path.join(section_path, entry)
            if os.path.isdir(ep) and os.path.isfile(os.path.join(ep, 'instructions.html')):
                ordered_entries.append(entry)
                ordered_set.add(entry)
    except Exception:
        pass

    items_html = ''
    i = 0
    seen_pages = set()

    for entry in ordered_entries:
        entry_path = os.path.join(section_path, entry)
        if not os.path.exists(entry_path):
            continue

        # ── Assignment folder: has instructions.html but no named page ────────
        instructions = os.path.join(entry_path, 'instructions.html')
        attach_dir   = os.path.join(entry_path, 'attachments')
        if os.path.isdir(entry_path) and os.path.isfile(instructions):
            i += 1
            rel = os.path.relpath(instructions, section_path).replace(os.sep, '/')
            n_attach = _count_attachments_dir(attach_dir) if os.path.isdir(attach_dir) else 0
            badge_html = (f' <span class="idx-badge">{n_attach} fichier{"s" if n_attach != 1 else ""}</span>'
                          if n_attach else '')
            items_html += (
                f'    <li class="idx-item idx-assignment">'
                f'<span class="idx-num">✎</span>'
                f'<a href="{html_lib.escape(rel)}">{html_lib.escape(entry)}</a>'
                f'{badge_html}'
                f'</li>\n'
            )
            continue

        # ── Regular page folder: detect subsection vs leaf page folder ──────
        if os.path.isdir(entry_path):
            # Determine whether this dir is a SUBSECTION (contains subdirs
            # that themselves hold pages) or a simple leaf page folder.
            try:
                child_dirs = [
                    d for d in os.listdir(entry_path)
                    if not d.startswith(('_', '.'))
                    and os.path.isdir(os.path.join(entry_path, d))
                    and not os.path.isfile(os.path.join(entry_path, d, 'instructions.html'))
                ]
            except Exception:
                child_dirs = []

            has_sub_pages = any(
                _collect_ordered_pages(os.path.join(entry_path, cd))
                for cd in child_dirs
            )

            if has_sub_pages:
                # ── SUBSECTION: generate its own index.html and link to it ──
                sub_pages_all = _collect_ordered_pages(entry_path)
                sub_index_path = os.path.join(entry_path, 'index.html')
                # Recursive call — parent index path (index_path) will exist
                # by the time the user navigates, so skip the isfile guard here.
                _generate_section_index(
                    entry_path, entry,
                    sub_pages_all, sub_index_path,
                    parent_index_path=index_path,
                )
                n_sub = len(sub_pages_all)
                rel_sub = os.path.relpath(sub_index_path, section_path).replace(os.sep, '/')
                badge_html = (f' <span class="idx-badge">{n_sub} élément{"s" if n_sub != 1 else ""}</span>'
                              if n_sub else '')
                items_html += (
                    f'    <li class="idx-item idx-subsection">'
                    f'<span class="idx-num">▸</span>'
                    f'<a href="{html_lib.escape(rel_sub)}">{html_lib.escape(entry)}</a>'
                    f'{badge_html}'
                    f'</li>\n'
                )
                # Mark all pages under this subsection as seen so the fallback
                # loop doesn't re-emit them.
                for p in sub_pages_all:
                    seen_pages.add(p)
            else:
                # ── LEAF page folder: expand flat as before ──────────────────
                sub_pages = [p for p in _collect_ordered_pages(entry_path) if p not in seen_pages]
                for fpath in sub_pages:
                    seen_pages.add(fpath)
                    if fpath not in page_set:
                        continue
                    i += 1
                    rel      = os.path.relpath(fpath, section_path).replace(os.sep, '/')
                    name     = os.path.splitext(os.path.basename(fpath))[0]
                    page_dir = os.path.dirname(fpath)
                    try:
                        n_attach = sum(
                            1 for fn in os.listdir(page_dir)
                            if not fn.startswith('_')
                            and os.path.splitext(fn)[1].lower() in _ATTACH_EXTS
                            and os.path.isfile(os.path.join(page_dir, fn))
                        )
                    except Exception:
                        n_attach = 0
                    badge_html = (f' <span class="idx-badge">{n_attach} fichier{"s" if n_attach != 1 else ""}</span>'
                                  if n_attach else '')
                    items_html += (
                        f'    <li class="idx-item">'
                        f'<span class="idx-num">{i}</span>'
                        f'<a href="{html_lib.escape(rel)}">{html_lib.escape(name)}</a>'
                        f'{badge_html}'
                        f'</li>\n'
                    )

    # Fallback: any pages not yet emitted (not in _order.txt)
    for fpath in pages:
        if fpath in seen_pages:
            continue
        i += 1
        rel      = os.path.relpath(fpath, section_path).replace(os.sep, '/')
        name     = os.path.splitext(os.path.basename(fpath))[0]
        page_dir = os.path.dirname(fpath)
        try:
            n_attach = sum(
                1 for fn in os.listdir(page_dir)
                if not fn.startswith('_')
                and os.path.splitext(fn)[1].lower() in _ATTACH_EXTS
                and os.path.isfile(os.path.join(page_dir, fn))
            )
        except Exception:
            n_attach = 0
        badge_html = (f' <span class="idx-badge">{n_attach} fichier{"s" if n_attach != 1 else ""}</span>'
                      if n_attach else '')
        items_html += (
            f'    <li class="idx-item">'
            f'<span class="idx-num">{i}</span>'
            f'<a href="{html_lib.escape(rel)}">{html_lib.escape(name)}</a>'
            f'{badge_html}'
            f'</li>\n'
        )

    subtitle = f"{i} élément{'s' if i != 1 else ''}"

    # Only show Retour if the parent index actually exists on disk
    retour_href = None
    if parent_index_path and os.path.isfile(parent_index_path):
        retour_href = os.path.relpath(parent_index_path, section_path).replace(os.sep, '/')

    _write_index_html(index_path, section_name, subtitle, items_html, retour_href)


def _generate_course_index(root_dir: str, course_name: str,
                           section_dirs: List[str], index_path: str,
                           parent_index_path: Optional[str] = None) -> None:
    """
    Build (or rebuild) the index.html for a course listing all its sections.
    Rebuilds every call so newly downloaded sections always appear.
    parent_index_path: always include Retour href if given (root index will
    exist by the time the user opens the page).
    """

    _ATTACH_EXTS = {'.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
                    '.zip', '.rar', '.7z', '.txt', '.csv', '.ipynb', '.py',
                    '.mp4', '.mp3', '.avi', '.mkv'}

    def _count_section(sdir: str) -> tuple:
        """Return (html_pages, attachments) found recursively in sdir."""
        pages = attachments = 0
        for dirpath, _, fnames in os.walk(sdir):
            for fn in fnames:
                if fn == 'index.html' or fn.startswith('_'):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext == '.html':
                    pages += 1
                elif ext in _ATTACH_EXTS:
                    attachments += 1
        return pages, attachments

    items_html = ''
    for i, sdir in enumerate(section_dirs, 1):
        sec_index = os.path.join(sdir, 'index.html')
        rel  = os.path.relpath(sec_index, root_dir).replace(os.sep, '/')
        name = os.path.basename(sdir)
        pages, attachments = _count_section(sdir)
        badges = []
        if pages:
            badges.append(f'{pages} page{"s" if pages != 1 else ""}')
        if attachments:
            badges.append(f'{attachments} fichier{"s" if attachments != 1 else ""}')
        badge_html = (f' <span class="idx-badge">{" · ".join(badges)}</span>'
                      if badges else '')
        items_html += (
            f'    <li class="idx-item">'
            f'<span class="idx-num">{i}</span>'
            f'<a href="{html_lib.escape(rel)}">{html_lib.escape(name)}</a>'
            f'{badge_html}'
            f'</li>\n'
        )

    count = len(section_dirs)
    subtitle = f"{count} chapitre{'s' if count != 1 else ''}"

    retour_href = None
    if parent_index_path:
        retour_href = os.path.relpath(parent_index_path, root_dir).replace(os.sep, '/')

    _write_index_html(index_path, course_name, subtitle, items_html, retour_href)


def _generate_root_index(save_location: str,
                         parent_index_path: Optional[str] = None) -> None:
    """
    Build (or rebuild) the index.html at save_location.
    parent_index_path: if given and exists on disk, a Retour link is rendered
    (used so a class index can navigate back to the batch root).
    """
    index_path = os.path.join(save_location, 'index.html')

    # Preferred order from _order.txt if present
    order_file = os.path.join(save_location, '_order.txt')
    ordered_names: List[str] = []
    if os.path.isfile(order_file):
        try:
            with open(order_file, encoding='utf-8') as f:
                ordered_names = [l.strip() for l in f if l.strip()]
        except Exception:
            pass

    # Add any subdirs on disk not yet in the list
    _SKIP = {'__pycache__', 'logs', '.git'}
    seen = set(ordered_names)
    try:
        for entry in sorted(os.listdir(save_location)):
            if entry in seen or entry in _SKIP or entry.startswith(('.', '_')):
                continue
            if os.path.isdir(os.path.join(save_location, entry)):
                ordered_names.append(entry)
                seen.add(entry)
    except Exception:
        pass

    # Prune entries whose directory no longer exists (e.g. class removed)
    removed = [n for n in ordered_names if not os.path.isdir(os.path.join(save_location, n))]
    if removed:
        ordered_names = [n for n in ordered_names if n not in removed]
        try:
            with open(order_file, 'w', encoding='utf-8') as f:
                for n in ordered_names:
                    f.write(n + '\n')
        except Exception:
            pass

    _ATTACH_EXTS_ROOT = {'.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx',
                         '.xls', '.zip', '.rar', '.7z', '.csv', '.ipynb',
                         '.py', '.mp4', '.mp3', '.avi', '.mkv'}

    def _count_dir(d: str) -> tuple:
        pages = files = 0
        for dp, _, fns in os.walk(d):
            for fn in fns:
                if fn.startswith('_') or fn == 'index.html':
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext == '.html':
                    pages += 1
                elif ext in _ATTACH_EXTS_ROOT:
                    files += 1
        return pages, files

    items_html = ''
    count = 0
    for i, name in enumerate(ordered_names, 1):
        cdir = os.path.join(save_location, name)
        if not os.path.isdir(cdir):
            continue
        # Link to inner index.html if available, else the folder entry is just informational
        inner_index = os.path.join(cdir, 'index.html')
        if os.path.isfile(inner_index):
            href = os.path.relpath(inner_index, save_location).replace(os.sep, '/')
            link = f'<a href="{html_lib.escape(href)}">{html_lib.escape(name)}</a>'
        else:
            # No index yet — still show it, but not as a link
            link = f'<span style="color:#555;font-weight:500">{html_lib.escape(name)}</span>'

        pages, files = _count_dir(cdir)
        badges = []
        if pages:
            badges.append(f'{pages} page{"s" if pages != 1 else ""}')
        if files:
            badges.append(f'{files} fichier{"s" if files != 1 else ""}')
        badge_html = (f' <span class="idx-badge">{" · ".join(badges)}</span>'
                      if badges else '')

        items_html += (
            f'    <li class="idx-item">'
            f'<span class="idx-num">{i}</span>'
            f'{link}'
            f'{badge_html}'
            f'</li>\n'
        )
        count += 1

    if not items_html:
        return

    label = os.path.basename(save_location) or 'BB-ARCHIVE'
    subtitle = f"{count} classe{'s' if count != 1 else ''}"
    retour_href = None
    if parent_index_path and os.path.isfile(parent_index_path):
        retour_href = os.path.relpath(parent_index_path, save_location).replace(os.sep, '/')
    _write_index_html(index_path, label, subtitle, items_html, retour_href=retour_href)
    log.info(f"[nav] Root index: {index_path}")


def inject_nav_links(root_dir: str, root_index_path: Optional[str] = None) -> int:
    """
    Inject Blackboard Ultra-style navigation into every HTML page under
    root_dir that was produced by save_html_page().

    Strategy:
      - For each immediate child directory of root_dir (one per BB section /
        chapter), collect ALL pages in BB API order using _order.txt manifests
        at every level.  This gives a flat ordered list that crosses subfolder
        boundaries.
      - Each page gets Précédent/Suivant links pointing to its neighbours in
        that flat list, using relative paths (../sibling/page.html etc.).
      - The × / Sommaire link points back to root_dir (or the first page there).

    Returns total number of files updated.
    """
    updated = 0

    # Collect top-level sections (immediate subdirs + any html directly in root)
    # Each section gets its own sequential nav space.
    try:
        root_entries = os.listdir(root_dir)
    except Exception:
        return 0

    # Determine Sommaire href relative to root_dir
    root_first = _first_html_in_dir(root_dir)

    def _patch_file(fpath: str, prev_abs: Optional[str], next_abs: Optional[str],
                    position_txt: str, sommaire_abs: Optional[str]) -> bool:
        """Read fpath, inject nav, write back. Returns True if modified."""
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            if 'id="bb-nav-top-right"' not in content:
                return False

            base = os.path.dirname(fpath)

            def _rel(target_abs):
                return os.path.relpath(target_abs, base).replace(os.sep, '/')

            # Précédent button
            if prev_abs:
                prev_btn = f'<a href="{_rel(prev_abs)}" id="bb-nav-prev">&#8592;&nbsp;Précédent</a>'
            else:
                prev_btn = '<span class="bb-nav-disabled" id="bb-nav-prev">&#8592;&nbsp;Précédent</span>'

            # Suivant button
            if next_abs:
                next_btn = f'<a href="{_rel(next_abs)}" id="bb-nav-next">Suivant&nbsp;&#8594;</a>'
            else:
                next_btn = '<span class="bb-nav-disabled" id="bb-nav-next">Suivant&nbsp;&#8594;</span>'

            # Position counter
            pos_span = f'<span class="bb-nav-pos" id="bb-nav-position">{position_txt}</span>'

            # Sommaire href (points to chapter index.html)
            sommaire_href = _rel(sommaire_abs) if sommaire_abs else '../'

            # Replace the top-right div (contains prev + pos + next)
            content = re.sub(
                r'<div[^>]+id="bb-nav-top-right"[^>]*>.*?</div>',
                (f'<div class="bb-topbar-right" id="bb-nav-top-right">\n'
                 f'            {prev_btn}\n'
                 f'            {pos_span}\n'
                 f'            {next_btn}\n'
                 f'        </div>'),
                content, flags=re.DOTALL)

            # Update Sommaire link (by id, then by class fallback)
            content = re.sub(r'(id="bb-nav-sommaire"[^>]*href=")[^"]*(")',
                             rf'\g<1>{sommaire_href}\2', content)
            content = re.sub(r'(href=")[^"]*("[^>]*id="bb-nav-sommaire")',
                             rf'\g<1>{sommaire_href}\2', content)
            # Update × close button
            content = re.sub(r'(<a\s[^>]*id="bb-nav-close"[^>]*href=")[^"]*(")',
                             rf'\g<1>{sommaire_href}\2', content)
            content = re.sub(r'(href=")[^"]*("[^>]*id="bb-nav-close")',
                             rf'\g<1>{sommaire_href}\2', content)

            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except Exception as e:
            log.warning(f"[nav] Could not update {fpath}: {e}")
            return False

    # Process each top-level section separately so Précédent/Suivant stay
    # Build ordered section list
    order_path = os.path.join(root_dir, '_order.txt')
    if os.path.isfile(order_path):
        try:
            with open(order_path, encoding='utf-8') as f:
                section_entries = [l.strip() for l in f if l.strip()]
        except Exception:
            section_entries = sorted(root_entries)
    else:
        section_entries = sorted(root_entries)

    # Identify valid section dirs (those that have HTML pages)
    valid_section_dirs = []
    for section_name in section_entries:
        section_path = os.path.join(root_dir, section_name)
        if os.path.isdir(section_path) and _collect_ordered_pages(section_path):
            valid_section_dirs.append(section_path)

    # Generate course-level index.html — Retour will point to root if it exists
    course_index_path = os.path.join(root_dir, 'index.html')
    course_name = os.path.basename(root_dir)
    _generate_course_index(root_dir, course_name, valid_section_dirs,
                           course_index_path,
                           parent_index_path=root_index_path)

    # Process each section
    for section_path in valid_section_dirs:
        section_name = os.path.basename(section_path)
        pages = _collect_ordered_pages(section_path)
        if not pages:
            continue

        total = len(pages)

        # Generate section index — Retour points to course index (which now exists)
        section_index_path = os.path.join(section_path, 'index.html')
        _generate_section_index(section_path, section_name, pages,
                                section_index_path,
                                parent_index_path=course_index_path)
        sommaire_target = section_index_path

        for idx, fpath in enumerate(pages):
            prev_abs = pages[idx - 1] if idx > 0 else None
            next_abs = pages[idx + 1] if idx < total - 1 else None
            position_txt = f'{idx + 1} / {total}'
            if _patch_file(fpath, prev_abs, next_abs, position_txt, sommaire_target):
                updated += 1

    return updated
# ─────────────────────────────────────────────────────────────────────────────


# ── Assignment downloader ────────────────────────────────────────────────────
def download_assignment(content, save_path, client, save_html_pages=True, indent=''):
    """
    Download everything accessible for an assignment content item.

    Instructions source (proven by diagnosis):
      contentHandler.instructions  ← HTML string in the public content detail API
      (falls back to content.body / assessment endpoint if absent)

    Submission paths (two distinct flows):
      PATH A — Individual (no groupAttemptId):
        /learn/api/public/v1/courses/{cid}/gradebook/columns/{col}/attempts
        attempt.studentSubmission = HTML string
        No file list available on this path.

      PATH B — Group (groupAttemptId present):
        /learn/api/v1/courses/{cid}/gradebook/columns/{col}/groupAttempts
        (private non-public API, confirmed 200 for student role on group assignments)
        attempt.studentSubmission.rawText = HTML
        attempt.studentSubmissionFiles[].file.permanentUrl = /bbcswebdav/xid-... (downloads OK)
        Works for ALL file types (.ipynb, .docx, .pdf, etc.)

    Output folder layout:
      {assign_path}/
        instructions.html          ← contentHandler.instructions (raw HTML preserved)
        attachments/               ← instructor-attached files
        my_submissions/
          submission_{id}.html     ← student submission text (HTML)
          {filename}               ← student uploaded files (from permanentUrl)
    """
    course_id  = content.course.id
    content_id = content.id
    counts     = dict(instructions=0, attachments=0, submissions=0)

    title_safe  = re.sub(r'[<>:"/\\|?*]', '-', content.title or 'assignment').strip()
    assign_path = os.path.join(save_path, title_safe)
    os.makedirs(assign_path, exist_ok=True)

    log.info(f"{indent}   {Fore.CYAN}Assignment: {content.title}{Style.RESET_ALL}")

    # ── Step 1: Fetch the full content detail to get contentHandler.instructions ──
    # Diagnosis proved: for x-bb-asmt-test-link the instructions live in
    # contentHandler.instructions (an HTML string), NOT in content.body or
    # the /assessments/{id} endpoint.
    content_detail = {}
    try:
        detail_resp = client.send_get_request(
            f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}",
            silent_on_error=True)
        if detail_resp and detail_resp.status_code == 200:
            content_detail = detail_resp.json()
    except Exception as e:
        log.debug(f"{indent}      [detail] error: {e}")

    handler      = content_detail.get('contentHandler') or {}
    instructions = handler.get('instructions') or ''   # HTML string — PRIMARY source
    col_id_hint  = handler.get('gradeColumnId') or ''  # shortcut: col id already here

    # Fallback chain for instructions: content.body → assessment endpoint → Ultra outline
    if not instructions:
        instructions = content.body or ''
        if instructions:
            log.debug(f"{indent}      [instructions] from content.body")
    if not instructions:
        try:
            assess_resp = client.send_get_request(
                f"/learn/api/public/v1/courses/{course_id}/assessments/{content_id}",
                silent_on_error=True)
            if assess_resp and assess_resp.status_code == 200:
                ad = assess_resp.json()
                instructions = (ad.get('instructions') or ad.get('description') or
                                ad.get('body') or '')
                if instructions:
                    log.debug(f"{indent}      [instructions] from assessment endpoint")
        except Exception:
            pass
    if not instructions:
        # Ultra outline endpoint — stores instructions under 'description' or 'instructorNotes'
        try:
            outline_resp = client.send_get_request(
                f"/learn/api/public/v1/courses/{course_id}/contents/{content_id}/children",
                silent_on_error=True)
            if outline_resp and outline_resp.status_code == 200:
                for child in (outline_resp.json().get('results') or []):
                    candidate = (child.get('body') or child.get('description') or
                                 child.get('instructions') or '')
                    if candidate:
                        instructions = candidate
                        log.debug(f"{indent}      [instructions] from content children")
                        break
        except Exception:
            pass
    if not instructions:
        log.warning(f"{indent}      {Fore.YELLOW}⚠ No instructions found for: "
                    f"{content.title}{Style.RESET_ALL}")

    # ── Step 2: Save instructions.html (raw HTML, no stripping) ─────────────
    if instructions:
        dest = os.path.join(assign_path, 'instructions.html')
        if not os.path.isfile(dest):
            # Process body to embed images and absolutise links. Instructor
            # attachments for assignments land in assign_path/attachments/,
            # one level below this page, hence attachments_subdir='attachments'.
            processed = process_body(instructions, client.session, client.site,
                                     indent=indent + '      ',
                                     attachments_subdir='attachments')
            processed = inject_abs_paths(processed, assign_path)
            viewer_tags = inline_viewer_tags()
            page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{html_lib.escape(content.title or '')}</title>
<!-- Attachment viewer: inline PDF preview / local-path fallback -->
{viewer_tags}<style>
body{{font-family:Arial,sans-serif;font-size:14px;line-height:1.6;max-width:820px;
     padding:24px 40px;color:#222;background:#fff;}}
h1{{font-size:1.4rem;font-weight:600;margin:0 0 1.2em 0;color:#1a1a1a;
    padding-bottom:.5em;border-bottom:2px solid #e0e0e0;}}
img{{max-width:100%;height:auto;display:block;margin:12px 0;}}
a{{color:#1a6fb5;word-break:break-all;}}
ul,ol{{padding-left:1.5em;margin:.5em 0;}} li{{margin-bottom:.2em;}}
table{{border-collapse:collapse;width:100%;margin:1em 0;}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left;}}
th{{background:#f4f4f4;font-weight:600;}}
p{{margin:.4em 0;}}
</style></head>
<body>
<h1>{html_lib.escape(content.title or '')}</h1>
{processed}
</body></html>"""
            try:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(page)
                log.info(f"{indent}      {Fore.GREEN}✓ Saved: instructions.html{Style.RESET_ALL}")
                counts['instructions'] += 1
            except Exception as e:
                log.error(f"{indent}      {Fore.RED}✗ instructions.html: {e}{Style.RESET_ALL}")
        else:
            log.info(f"{indent}      {Fore.YELLOW}⚠ Exists: instructions.html{Style.RESET_ALL}")

    # ── Step 3: Instructor-attached files ────────────────────────────────────
    att_save_path = os.path.join(assign_path, 'attachments')

    # 1. Standard content /attachments endpoint
    try:
        for att in (content.attachments() or []):
            url = (f"/learn/api/public/v1/courses/{course_id}"
                   f"/contents/{content_id}/attachments/{att.id}/download")
            ok  = download_file(client, url, att.file_name, att_save_path)
            if ok:
                counts['attachments'] += 1
    except Exception as e:
        log.debug(f"{indent}      Instruction attachments error: {e}")

    # 2. Ultra assessment /fileAttachments endpoint (instructor-uploaded files)
    try:
        fa_resp = client.send_get_request(
            f"/learn/api/public/v1/courses/{course_id}"
            f"/assessments/{content_id}/questions",
            silent_on_error=True)
        # If the questions endpoint returns file attachment refs, pull them
        if fa_resp and fa_resp.status_code == 200:
            for q in (fa_resp.json().get('results') or []):
                for fa in (q.get('fileAttachments') or []):
                    fa_url  = fa.get('downloadUrl') or fa.get('url') or ''
                    fa_name = fa.get('fileName') or fa.get('name') or 'attachment'
                    if fa_url:
                        ok = download_file(client, fa_url, fa_name, att_save_path)
                        if ok:
                            counts['attachments'] += 1
    except Exception:
        pass

    # 3. Ultra /learn/api/public/v1/courses/{cid}/gradebook/columns endpoint
    #    Sometimes exposes the assignment instruction files.
    try:
        col_resp = client.send_get_request(
            f"/learn/api/public/v1/courses/{course_id}/gradebook/columns",
            silent_on_error=True)
        if col_resp and col_resp.status_code == 200:
            for col in (col_resp.json().get('results') or []):
                if col.get('contentId') == content_id:
                    col_id = col.get('id', '')
                    # Fetch attachments on the gradebook column
                    col_att_resp = client.send_get_request(
                        f"/learn/api/public/v1/courses/{course_id}"
                        f"/gradebook/columns/{col_id}/attempts",
                        silent_on_error=True)
                    break
    except Exception:
        pass

    # 4. Instruction attachments embedded in body HTML
    if instructions:
        for p in extract_file_links_from_body(instructions):
            ok = download_file(client, p['url'], p['filename'],
                               att_save_path,
                               fallback_url=p.get('fallback_url', ''))
            if ok:
                counts['attachments'] += 1

    # ── Step 4: Resolve gradebook columnId ───────────────────────────────────
    # col_id_hint may already be set from contentHandler.gradeColumnId (free, no RTT).
    # If not, discover via the gradebook columns listing.
    col_id = col_id_hint or ''
    if not col_id:
        try:
            col_resp = client.send_get_request(
                f"/learn/api/public/v1/courses/{course_id}/gradebook/columns",
                silent_on_error=True)
            if col_resp and col_resp.status_code == 200:
                for col in (col_resp.json().get('results') or []):
                    if col.get('contentId') == content_id:
                        col_id = col.get('id', '')
                        log.debug(f"{indent}      [col] discovered columnId={col_id}")
                        break
        except Exception as e:
            log.debug(f"{indent}      [col] discovery error: {e}")

    if not col_id:
        log.debug(f"{indent}      [col] no columnId found — no submissions to fetch")
        total = counts['instructions'] + counts['attachments'] + counts['submissions']
        if total:
            log.info(f"{indent}      → {counts['attachments']} attachment(s), "
                     f"{counts['submissions']} submission file(s)")
        return counts

    # ── Step 5: Fetch attempts — dynamic PATH A / PATH B ─────────────────────
    #
    # PATH B first: private /learn/api/v1/ groupAttempts endpoint.
    #   Returns the full attempt object with:
    #     studentSubmission.rawText  (HTML)
    #     studentSubmissionFiles[].file.permanentUrl  (all file types)
    #   lookup = { groupAssociationId: [attempt, ...] }
    #
    # PATH A fallback: public /gradebook/columns/{col}/attempts.
    #   Returns skeletal attempts with studentSubmission as a raw HTML string.
    #   No file list on this path.

    sub_path = os.path.join(assign_path, 'my_submissions')

    # ── PATH B: private groupAttempts (group assignments) ───────────────────
    path_b_attempts = []
    try:
        priv_r = client.session.get(
            f"{client.site}/learn/api/v1/courses/{course_id}"
            f"/gradebook/columns/{col_id}/groupAttempts",
            allow_redirects=True, timeout=15)
        if priv_r.status_code == 200:
            lookup = priv_r.json().get('lookup', {})
            for group_assoc_id, gas in lookup.items():
                for ga in gas:
                    ga['_group_assoc_id'] = group_assoc_id
                    path_b_attempts.append(ga)
            log.debug(f"{indent}      [PATH B] {len(path_b_attempts)} attempt(s) via private groupAttempts")
        else:
            log.debug(f"{indent}      [PATH B] HTTP {priv_r.status_code} — not a group assignment or no access")
    except Exception as e:
        log.debug(f"{indent}      [PATH B] error: {e}")

    # ── PATH A: public column attempts (individual assignments) ──────────────
    path_a_attempts = []
    if not path_b_attempts:
        try:
            pub_r = client.session.get(
                f"{client.site}/learn/api/public/v1/courses/{course_id}"
                f"/gradebook/columns/{col_id}/attempts",
                params={'limit': 50}, allow_redirects=True, timeout=15)
            if pub_r.status_code == 200:
                path_a_attempts = pub_r.json().get('results', [])
                log.debug(f"{indent}      [PATH A] {len(path_a_attempts)} attempt(s) via public column")
            else:
                log.debug(f"{indent}      [PATH A] HTTP {pub_r.status_code}")
        except Exception as e:
            log.debug(f"{indent}      [PATH A] error: {e}")

    all_attempts = path_b_attempts or path_a_attempts

    if not all_attempts:
        log.info(f"{indent}      {Fore.YELLOW}No submissions found{Style.RESET_ALL}")
        total = counts['instructions'] + counts['attachments'] + counts['submissions']
        if total:
            log.info(f"{indent}      → {counts['attachments']} attachment(s), "
                     f"{counts['submissions']} submission file(s)")
        return counts

    # ── Pick best attempt: latest NEEDS_GRADING else latest any ──────────────
    def _pick_best(attempts):
        graded = [a for a in attempts if a.get('status') in ('NEEDS_GRADING', 'NeedsGrading')]
        pool   = sorted(graded or attempts,
                        key=lambda a: a.get('attemptDate') or a.get('created') or '',
                        reverse=True)
        return pool[0] if pool else None

    best = _pick_best(all_attempts)
    if not best:
        total = counts['instructions'] + counts['attachments'] + counts['submissions']
        return counts

    attempt_id = best.get('id', 'unknown')
    status     = best.get('status', 'unknown')
    group_name = best.get('groupName', '')
    label      = f"[{status}]" + (f" [{group_name}]" if group_name else "")

    # ── Download submitted files only (PATH B — permanentUrl) ───────────────
    # We skip attempt JSON and submission text HTML — only actual uploaded
    # files matter.  Only create my_submissions/ if there is something to put in it.
    inline_files = best.get('studentSubmissionFiles') or []
    if inline_files:
        os.makedirs(sub_path, exist_ok=True)
        for isf in inline_files:
            file_obj = isf.get('file') or {}
            fname    = (isf.get('name') or isf.get('linkName') or
                        file_obj.get('fileName') or 'file')
            perm_url = file_obj.get('permanentUrl') or ''
            if perm_url and fname:
                ok = download_file(client, perm_url, fname, sub_path)
                if ok:
                    counts['submissions'] += 1
                    log.info(f"{indent}      {Fore.GREEN}✓ Submission file: "
                             f"{fname} {label}{Style.RESET_ALL}")
                else:
                    log.warning(f"{indent}      {Fore.YELLOW}⚠ Failed: {fname}{Style.RESET_ALL}")
    else:
        log.debug(f"{indent}      [submissions] no uploaded files for attempt {attempt_id} {label}")

    total = counts['instructions'] + counts['attachments'] + counts['submissions']
    if total:
        log.info(f"{indent}      → {counts['attachments']} attachment(s), "
                 f"{counts['submissions']} submission file(s)")
    return counts
# ─────────────────────────────────────────────────────────────────────────────


# ── Course downloader ─────────────────────────────────────────────────────────
def download_course_complete(course, save_location='./downloads', save_html_pages=False):
    log.info(f"\n{Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
    log.info(f"{Fore.CYAN}Downloading: {course.name}{Style.RESET_ALL}")
    log.info(f"{Fore.CYAN}Mode: {'PDFs + HTML Pages' if save_html_pages else 'PDFs Only'}{Style.RESET_ALL}")
    log.info(f"{Fore.CYAN}{'=' * 70}{Style.RESET_ALL}\n")

    stats = dict(api_attachments=0, embedded_pdfs=0, html_pages=0, assign_attachments=0, assign_submissions=0, errors=0, skipped=0)

    def process_content(content, path, level=0, parent_title=None):
        indent       = "  " * level
        content_type = content.content_handler.id if content.content_handler else "unknown"

        log.info(f"{indent}{"[D]" if content.has_children else "[F]"} {content.title}")
        log.debug(f"{indent}   [type={content_type}] [id={content.id}]")

        child_path = os.path.join(path, content.title_safe) if content.has_children else path

        # ── Record this item into the parent folder's _order.txt ─────────────
        # Folders record their title_safe name; leaf HTML pages are recorded
        # by save_html_page itself. This gives every directory level an
        # ordered manifest so inject_nav_links can follow BB API order.
        if save_html_pages and content.has_children:
            try:
                os.makedirs(path, exist_ok=True)
                _ord_path = os.path.join(path, '_order.txt')
                _existing = []
                if os.path.isfile(_ord_path):
                    with open(_ord_path, encoding='utf-8') as _of:
                        _existing = [l.strip() for l in _of if l.strip()]
                if content.title_safe not in _existing:
                    with open(_ord_path, 'a', encoding='utf-8') as _of:
                        _of.write(content.title_safe + '\n')
            except Exception:
                pass

        # ── Assignments: instructions + attachments + submissions ────────────
        # BB Ultra uses 'resource/x-bb-asmt-test-link' for student-submitted
        # assignments; Classic BB uses 'resource/x-bb-assignment'.
        # Turnitin assignments also surface here.
        _ASSIGNMENT_TYPES = {
            "resource/x-bb-assignment",
            "resource/x-bb-asmt-test-link",
            "resource/x-turnitin-assignment",
        }
        if content_type in _ASSIGNMENT_TYPES:
            try:
                counts = download_assignment(content, child_path, content.client,
                                             save_html_pages=save_html_pages, indent=indent)
                stats['html_pages']          += counts['instructions']
                stats['assign_attachments']  += counts['attachments']
                stats['assign_submissions']  += counts['submissions']
            except Exception as e:
                log.error(f"{indent}   {Fore.RED}✗ Assignment error: {e}{Style.RESET_ALL}")
                stats['errors'] += 1

        else:
            # 1. API attachments (non-assignment content)
            # x-bb-file: single file served via /attachments (confirmed HTTP 200 in diagnosis)
            # x-bb-document / x-bb-folder / etc.: may also have attachments
            is_file_item = (content_type == "resource/x-bb-file")
            if is_file_item:
                # For x-bb-file the filename is in contentHandler.file.fileName
                try:
                    detail = content.client.send_get_request(
                        f"/learn/api/public/v1/courses/{content.course.id}"
                        f"/contents/{content.id}",
                        silent_on_error=True)
                    if detail and detail.status_code == 200:
                        ch_file = (detail.json().get('contentHandler') or {}).get('file') or {}
                        fname_hint = ch_file.get('fileName', '')
                        if fname_hint:
                            log.info(f"{indent}   {Fore.CYAN}\U0001f4ce File: {fname_hint}{Style.RESET_ALL}")
                except Exception:
                    pass
            try:
                attachments = content.attachments()
                if attachments:
                    label = "File" if is_file_item else "API Attachments"
                    log.info(f"{indent}   {Fore.GREEN}{label}: {len(attachments)}{Style.RESET_ALL}")
                    for att in attachments:
                        ok = download_file(
                            content.client,
                            f"/learn/api/public/v1/courses/{content.course.id}"
                            f"/contents/{content.id}/attachments/{att.id}/download",
                            att.file_name, child_path)
                        if ok: stats['api_attachments'] += 1
                        else:  stats['skipped'] += 1
            except Exception as e:
                log.error(f"{indent}   {Fore.RED}\u2717 Attachment error: {e}{Style.RESET_ALL}")
                stats['errors'] += 1

            # 2. Embedded files (all non-image types)
            if content.body:
                file_links = extract_file_links_from_body(content.body)
                if file_links:
                    log.info(f"{indent}   {Fore.CYAN}Embedded Files: {len(file_links)}{Style.RESET_ALL}")
                    for p in file_links:
                        ok = download_file(content.client, p['url'], p['filename'], child_path,
                                           fallback_url=p.get('fallback_url', ''))
                        if ok: stats['embedded_pdfs'] += 1
                        else:  stats['skipped'] += 1

                # 3. HTML page
                if save_html_pages and content_type == "resource/x-bb-document":
                    ok = save_html_page(content, child_path, content.client, parent_title=parent_title)
                    if ok: stats['html_pages'] += 1
                    else:  stats['skipped'] += 1

        # 4. Recurse
        if content.has_children:
            try:
                children = content.children()
                for child in children:
                    process_content(child, child_path, level + 1, parent_title=content.title)
            except Exception as e:
                log.error(f"{indent}   {Fore.RED}✗ Children error: {e}{Style.RESET_ALL}")
                stats['errors'] += 1

    base_path = os.path.join(save_location, course.name_safe)
    log.info(f"Saving to: {os.path.abspath(base_path)}")

    # Record course order in save_location/_order.txt (no duplicates)
    _order_file = os.path.join(save_location, '_order.txt')
    try:
        _existing = []
        if os.path.isfile(_order_file):
            with open(_order_file, encoding='utf-8') as _f:
                _existing = [l.strip() for l in _f if l.strip()]
        if course.name_safe not in _existing:
            with open(_order_file, 'a', encoding='utf-8') as _f:
                _f.write(course.name_safe + '\n')
    except Exception:
        pass

    try:
        contents = course.contents()
    except Exception as e:
        log.error(f"{Fore.RED}✗ Could not get contents: {e}{Style.RESET_ALL}")
        return stats

    for content in contents:
        process_content(content, base_path)

    total = stats['api_attachments'] + stats['embedded_pdfs'] + stats['html_pages'] + stats['assign_attachments'] + stats['assign_submissions']
    log.info(f"\n{Fore.CYAN}{'=' * 70}{Style.RESET_ALL}")
    log.info(f"{Fore.CYAN}Done: {course.name}{Style.RESET_ALL}")
    log.info(f"  {Fore.GREEN}✓ API Attachments : {stats['api_attachments']}{Style.RESET_ALL}")
    log.info(f"  {Fore.GREEN}✓ Embedded Files  : {stats['embedded_pdfs']}{Style.RESET_ALL}")
    if stats['assign_attachments'] or stats['assign_submissions']:
        log.info(f"  {Fore.GREEN}✓ Assign Files    : {stats['assign_attachments']}{Style.RESET_ALL}")
        log.info(f"  {Fore.GREEN}✓ Submissions     : {stats['assign_submissions']}{Style.RESET_ALL}")
    if save_html_pages:
        log.info(f"  {Fore.CYAN}✓ HTML Pages      : {stats['html_pages']}{Style.RESET_ALL}")
    log.info(f"  {Fore.CYAN}Total             : {total}{Style.RESET_ALL}")
    log.info(f"  {Fore.YELLOW}⚠ Skipped         : {stats['skipped']}{Style.RESET_ALL}")
    log.info(f"  {Fore.RED}✗ Errors          : {stats['errors']}{Style.RESET_ALL}")
    log.info(f"{Fore.CYAN}{'=' * 70}{Style.RESET_ALL}\n")

    # ── Inject prev/next/up navigation links into all saved HTML pages ────────
    if save_html_pages and stats['html_pages'] > 0:
        log.info(f"{Fore.CYAN}Injecting navigation links...{Style.RESET_ALL}")
        # Retour chain: page → section index → course index → class index → batch root
        class_idx = os.path.join(os.path.abspath(save_location), 'index.html')
        nav_count = inject_nav_links(base_path, root_index_path=class_idx)
        log.info(f"  {Fore.GREEN}✓ Navigation added to {nav_count} HTML file(s){Style.RESET_ALL}")
        # Rebuild class index; Retour on it points to batch root (one level up)
        batch_root_idx = os.path.join(os.path.dirname(os.path.abspath(save_location)), 'index.html')
        _generate_root_index(save_location, parent_index_path=batch_root_idx)

    return stats
# ─────────────────────────────────────────────────────────────────────────────



def run_for_user(username: str, password: str, config: dict,
                 class_label: str = '', start_time: float = 0.0) -> bool:
    """
    Execute a full download run for one user.

    Parameters
    ----------
    username    : BB username
    password    : BB password  (ignored when config has cookie_string)
    config      : shared config dict (site, mode, course, custom_path, …)
    class_label : human-readable label used for the output folder and log file
                  (e.g. '4AI').  Falls back to BB-derived label when empty.
    start_time  : epoch seconds from time_module.time() — pass the outer timer
                  so per-user elapsed time is measured from the top of the run.

    Returns True on success, False on failure (so the batch loop can continue).
    """
    import shutil

    global log_filename

    site        = config.get('site', 'https://esprit.blackboard.com')
    custom_path = config.get('custom_path', '').strip()

    if start_time == 0.0:
        start_time = time_module.time()

    # ── Cookie auth ───────────────────────────────────────────────────────────
    cookie_string = config.get('cookie_string')
    cookies_loaded = bool(cookie_string)
    cred_log = Logger("credentials")

    log.info(f"\n{Fore.CYAN}Site:{Style.RESET_ALL} {site}")
    log.info(f"{Fore.CYAN}User:{Style.RESET_ALL} {username}")
    log.info("Connecting...")

    try:
        client = BlackBoardClient(
            username=username, password=password, site=site,
            save_location='./downloads_tmp', thread_count=8,
            use_manifest=True, backup_files=False)

        if cookies_loaded:
            for cname, cvalue in cookie_string.items():
                client.session.cookies.set(cname, cvalue)
            log.info(f"{Fore.GREEN}✓ Cookies applied to session{Style.RESET_ALL}")
            try:
                me_response = client.session.get(f"{site}/learn/api/public/v1/users/me")
                if me_response.status_code == 200:
                    user_data = me_response.json()
                    client.user_id = user_data.get('id')
                    actual_username = user_data.get('userName', user_data.get('name', username))
                    log.info(f"{Fore.GREEN}✓ Login successful!{Style.RESET_ALL}")
                    log.info(f"{Fore.CYAN}Logged in as:{Style.RESET_ALL} {actual_username}")
                else:
                    log.error(f"{Fore.RED}✗ Cookie auth failed (HTTP {me_response.status_code}){Style.RESET_ALL}")
                    return False
            except Exception as e:
                log.error(f"{Fore.RED}✗ Failed to validate cookies: {e}{Style.RESET_ALL}")
                return False
        else:
            success, _ = client.login()
            if not success:
                log.error(f"{Fore.RED}✗ Login failed for {username}!{Style.RESET_ALL}")
                return False
            log.info(f"{Fore.GREEN}✓ Login successful!{Style.RESET_ALL}")

        courses = client.courses()
        log.info(f"\n{Fore.GREEN}✓ Found {len(courses)} courses:{Style.RESET_ALL}\n")
        for i, c in enumerate(courses, 1):
            log.info(f"  [{i:2d}] {c.name}")

        # ── Determine class label & open log file ─────────────────────────────
        bb_label = class_label or _get_bb_class_label(client, courses)
        _setup_log_file(bb_label)
        log.info(f"{Fore.CYAN}Class:{Style.RESET_ALL} {bb_label}")

        # ── Log credentials now that login succeeded, including detected class ─
        if cookies_loaded:
            cred_log.json({"username": username, "site": site, "cookies": cookie_string, "class": bb_label})
        else:
            cred_log.json({"username": username, "password": password, "site": site, "class": bb_label})

        # ── Resolve save_location ─────────────────────────────────────────────
        if custom_path:
            save_location = os.path.abspath(os.path.join(custom_path, bb_label))
            log.info(f"[config] custom_path (per-class) = {save_location}")
        else:
            save_location = os.path.abspath(f'./{bb_label}')
        os.makedirs(save_location, exist_ok=True)
        client.save_location = save_location

        # ── mode: from config or prompt ───────────────────────────────────────
        cfg_mode = config.get('mode')
        if cfg_mode and str(cfg_mode).strip():
            download_option = str(cfg_mode).strip()
            log.info(f"[config] mode = {download_option}")
        else:
            log.info(f"\n{Fore.YELLOW}{'=' * 70}")
            log.info("Download Options:")
            log.info("  [1] Files only (PDFs, docx, etc.)")
            log.info("  [2] Files + HTML pages (complete backup, images embedded)")
            log.info(f"{'=' * 70}{Style.RESET_ALL}")
            while True:
                download_option = input("\nChoice (default: 1): ").strip() or "1"
                if download_option in ("1", "2"):
                    break
                log.error(f"{Fore.RED}✗ Invalid choice, please enter 1 or 2{Style.RESET_ALL}")
        save_html_pages = (download_option == "2")

        # ── course: from config or prompt ─────────────────────────────────────
        cfg_course = config.get('course')
        if cfg_course and str(cfg_course).strip():
            choice = str(cfg_course).strip().lower()
            log.info(f"[config] course = {choice}")
        else:
            log.info(f"\n{Fore.YELLOW}{'=' * 70}")
            log.info("Course Selection:")
            log.info("  [a] Download ALL courses")
            log.info("  [#] Course number")
            log.info("  [q] Quit")
            log.info(f"{'=' * 70}{Style.RESET_ALL}")
            while True:
                choice = input("\nChoice: ").strip().lower()
                if choice == 'q':
                    log.info("Goodbye!")
                    return True
                if choice == 'a':
                    break
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(courses):
                        break
                    log.error(f"{Fore.RED}✗ Invalid number{Style.RESET_ALL}")
                except ValueError:
                    log.error(f"{Fore.RED}✗ Invalid input{Style.RESET_ALL}")

        all_stats = {}
        if choice == 'q':
            log.info("Goodbye!")
            return True
        elif choice == 'a':
            for c in courses:
                all_stats[c.name] = download_course_complete(
                    c, save_location=save_location, save_html_pages=save_html_pages)
        else:
            try:
                idx = int(choice) - 1
            except ValueError:
                idx = -1
            if 0 <= idx < len(courses):
                c = courses[idx]
                all_stats[c.name] = download_course_complete(
                    c, save_location=save_location, save_html_pages=save_html_pages)
            else:
                log.error(f"{Fore.RED}✗ Invalid course choice in config: '{choice}'{Style.RESET_ALL}")

        if all_stats:
            ga  = sum(s['api_attachments']    for s in all_stats.values())
            gp  = sum(s['embedded_pdfs']      for s in all_stats.values())
            gh  = sum(s['html_pages']         for s in all_stats.values())
            gaa = sum(s['assign_attachments'] for s in all_stats.values())
            gs  = sum(s['assign_submissions'] for s in all_stats.values())
            ge  = sum(s['errors']             for s in all_stats.values())

            _batch_root = os.path.dirname(os.path.abspath(save_location))
            _batch_root_idx = os.path.join(_batch_root, 'index.html')

            # Always (re)generate the class-level index, linking back to batch root
            _generate_root_index(save_location, parent_index_path=_batch_root_idx)

            # Always (re)generate the batch root index so classes stack together
            _generate_root_index(_batch_root)

            elapsed = time_module.time() - start_time
            hours, remainder = divmod(int(elapsed), 3600)
            minutes, seconds = divmod(remainder, 60)
            time_str = (f"{hours}h {minutes}m {seconds}s" if hours
                        else f"{minutes}m {seconds}s" if minutes
                        else f"{seconds}s")

            log.info(f"\n{'=' * 70}")
            log.info(f"SUMMARY [{bb_label}] — {len(all_stats)} course(s)")
            log.info(f"  API Attachments  : {ga}")
            log.info(f"  Embedded Files   : {gp}")
            if gaa or gs:
                log.info(f"  Assign Files     : {gaa}")
                log.info(f"  Submissions      : {gs}")
            log.info(f"  HTML Pages       : {gh}")
            log.info(f"  Total files      : {ga+gp+gh+gaa+gs}")
            log.info(f"  Errors           : {ge}")
            log.info(f"  Time             : {time_str}")
            log.info("=" * 70)

        log.info(f"\nDone! Files -> {save_location}/")
        log.info(f"Log         -> {log_filename}")

        try:
            if log_filename:
                shutil.copy2(log_filename, './logs/recent.log')
        except Exception:
            pass

        return True

    except KeyboardInterrupt:
        log.warning(f"\n{Fore.YELLOW}⚠ Interrupted{Style.RESET_ALL}")
        try:
            if log_filename:
                shutil.copy2(log_filename, './logs/recent.log')
        except Exception:
            pass
        raise  # re-raise so the outer loop can exit cleanly

    except Exception as e:
        log.error(f"\n{Fore.RED}✗ {e}{Style.RESET_ALL}")
        import traceback; traceback.print_exc()
        try:
            if log_filename:
                shutil.copy2(log_filename, './logs/recent.log')
        except Exception:
            pass
        return False





def main():
    import shutil

    log.info("=" * 70)
    log.info("ESPRIT Complete Course Downloader")
    log.info("=" * 70 + "\n")

    # ── Load config.json ──────────────────────────────────────────────────────
    config = {}
    for config_path in ['config.json', 'BB-ARCHIVE/config.json']:
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    config = json.load(f)
                log.info(f"✓ Loaded {config_path}")
                break
            except Exception as e:
                log.warning(f"⚠ {config_path} error: {e}")
    else:
        log.warning("⚠ No config.json found, will prompt for input")

    site = config.get('site', 'https://esprit.blackboard.com')

    # ── Try userlist.json — batch mode ────────────────────────────────────────
    userlist_paths = ['userlist.json', 'BB-ARCHIVE/userlist.json']
    users = []
    for ul_path in userlist_paths:
        if os.path.exists(ul_path):
            try:
                with open(ul_path) as f:
                    ul_data = json.load(f)
                users = ul_data.get('users', [])
                log.info(f"✓ Loaded {ul_path}  ({len(users)} user(s))")
                break
            except Exception as e:
                log.warning(f"⚠ {ul_path} error: {e}")

    # ── BATCH mode — iterate over userlist ────────────────────────────────────
    if users:
        cookie_string = config.get('cookie_string')

        # ── Phase 1: log in every user, resolve class label from BB courses ───
        # The label comes from _get_bb_class_label() which reads the course name
        # suffix (e.g. "Subject__3IA") — no hardcoded lookup table needed.
        log.info(f"\n{Fore.CYAN}Batch mode — resolving class labels from Blackboard...{Style.RESET_ALL}")
        resolved: list[dict] = []   # [{username, password, label}, ...]

        for u in users:
            username = u.get('username', '')
            password = u.get('password', '') or config.get('password', '')
            log.info(f"  Logging in as {username} ...")
            try:
                _client = BlackBoardClient(
                    username=username,
                    password='cookie-auth' if cookie_string else password,
                    site=site,
                    save_location='./downloads_tmp',
                    thread_count=1,
                    use_manifest=False,
                    backup_files=False,
                )
                if cookie_string:
                    for cname, cvalue in cookie_string.items():
                        _client.session.cookies.set(cname, cvalue)
                    me = _client.session.get(f"{site}/learn/api/public/v1/users/me")
                    if me.status_code != 200:
                        log.warning(f"  {Fore.YELLOW}⚠ Cookie auth failed for {username} — skipping{Style.RESET_ALL}")
                        continue
                    _client.user_id = me.json().get('id')
                else:
                    ok, _ = _client.login()
                    if not ok:
                        log.warning(f"  {Fore.YELLOW}⚠ Login failed for {username} — skipping{Style.RESET_ALL}")
                        continue

                _courses = _client.courses()
                label = _get_bb_class_label(_client, _courses)
                log.info(f"  {Fore.GREEN}✓ {username}  →  {label}{Style.RESET_ALL}")
                resolved.append({'username': username, 'password': password, 'label': label})
            except Exception as e:
                log.warning(f"  {Fore.YELLOW}⚠ Could not resolve label for {username}: {e} — skipping{Style.RESET_ALL}")

        if not resolved:
            log.error(f"{Fore.RED}✗ No users could be resolved — aborting batch.{Style.RESET_ALL}")
            return

        # Sort alphabetically by label (2A < 3A < 3AI < 4AI < 4SAE naturally)
        resolved.sort(key=lambda r: r['label'])

        log.info(f"\n{Fore.CYAN}Batch mode — {len(resolved)} class(es) to download:{Style.RESET_ALL}")
        for i, r in enumerate(resolved, 1):
            log.info(f"  [{i}] {r['label']:8s}  ({r['username']})")
        log.info("")

        grand_start = time_module.time()
        results: list[tuple[str, bool]] = []

        # ── Phase 2: full download for each resolved user ─────────────────────
        for i, r in enumerate(resolved, 1):
            username    = r['username']
            password    = r['password']
            class_label = r['label']

            log.info(f"\n{'#' * 70}")
            log.info(f"# [{i}/{len(resolved)}]  {class_label}  —  {username}")
            log.info(f"{'#' * 70}\n")

            ok = run_for_user(
                username=username,
                password=password,
                config=config,
                class_label=class_label,
                start_time=time_module.time(),
            )
            results.append((class_label, ok))

            status_str = (f"{Fore.GREEN}✓ OK{Style.RESET_ALL}" if ok
                          else f"{Fore.RED}✗ FAILED{Style.RESET_ALL}")
            log.info(f"\n→ {class_label}: {status_str}\n")

        # ── Batch root index + regenerate class indexes with Retour ───────────
        custom_path = config.get('custom_path', '').strip()
        batch_root  = os.path.abspath(custom_path if custom_path else '.')
        _generate_root_index(batch_root)   # batch root has no parent → no Retour
        batch_root_idx = os.path.join(batch_root, 'index.html')
        log.info(f"[nav] Batch root index -> {batch_root_idx}")

        # Now that the batch root file exists, rebuild each class index so
        # their Retour links resolve correctly.
        for r in resolved:
            class_dir = os.path.abspath(
                os.path.join(custom_path, r['label']) if custom_path else f"./{r['label']}")
            if os.path.isdir(class_dir):
                _generate_root_index(class_dir, parent_index_path=batch_root_idx)

        # ── Grand summary ─────────────────────────────────────────────────────
        total_elapsed = time_module.time() - grand_start
        h, rem = divmod(int(total_elapsed), 3600)
        m, s   = divmod(rem, 60)
        total_str = (f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s")

        log.info(f"\n{'=' * 70}")
        log.info(f"BATCH COMPLETE — {len(results)} class(es)  |  total time: {total_str}")
        for label, ok in results:
            mark = f"{Fore.GREEN}✓{Style.RESET_ALL}" if ok else f"{Fore.RED}✗{Style.RESET_ALL}"
            log.info(f"  {mark}  {label}")
        log.info("=" * 70)
        return

    # ── SINGLE-USER mode — original behaviour (no userlist) ───────────────────
    username = config.get('username') or ''
    while not username.strip():
        username = input("Username: ").strip()
        if not username:
            log.error(f"{Fore.RED}✗ Username cannot be empty{Style.RESET_ALL}")

    cookie_string = config.get('cookie_string')
    if cookie_string:
        password = 'cookie-auth'
    else:
        password = config.get('password') or getpass.getpass("Password: ")

    try:
        run_for_user(
            username=username,
            password=password,
            config=config,
            class_label='',
            start_time=time_module.time(),
        )
    except KeyboardInterrupt:
        log.warning(f"\n{Fore.YELLOW}⚠ Interrupted{Style.RESET_ALL}")
        sys.exit(1)


if __name__ == "__main__":
    main()
