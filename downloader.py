import sys
import os
import re
import html
import shutil
import hashlib
import struct
import requests
import urllib3
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import mimetypes

# Ensure UTF-8 output on Windows consoles to prevent UnicodeEncodeError with emojis
if sys.platform == 'win32':
    try:
        if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class WebsiteDownloader:
    JUNK_DOMAINS = [
        'google-analytics.com',
        'googletagmanager.com',
        'google.com/recaptcha',
        'gstatic.com/recaptcha',
        'recaptcha.net',
        'hcaptcha.com',
        'challenges.cloudflare.com',
        'tiqcdn.com',
        'tealium',
        'hotjar.com',
        'clarity.ms',
        'segment.com',
        'segment.io',
        'mixpanel.com',
        'amplitude.com',
        'datadoghq-browser',
        'browser-agent.datadoghq',
        'browser.sentry-cdn.com',
        'sentry.io',
        'newrelic.com',
        'nr-data.net',
        'connect.facebook.net',
        'fbevents.js',
        'analytics.tiktok.com',
        'bat.bing.com',
        'criteo.com',
        'doubleclick.net',
        'adservice.google.com',
        'scorecardresearch.com',
        'adroll.com',
        'ads-twitter.com',
        'outbrain.com',
        'taboola.com',
    ]

    JUNK_SRC_PATTERNS = [
        r'recaptcha',
        r'grecaptcha',
        r'hcaptcha',
        r'turnstile',
        r'gtag',
        r'google-analytics',
        r'analytics(\.min)?\.js',
        r'utag(\.js|_)',
        r'fbevents',
        r'telemetry',
        r'beacon',
    ]

    def __init__(self, url, output_dir, log_callback=None):
        self.url = url
        self.output_dir = output_dir
        self.assets_dir = os.path.join(output_dir, 'assets')
        self.resource_cache = {}  # url -> local_path
        self.content_cache = {}   # sha256 -> local_path (deduplication)
        self.used_names = {}      # subdir -> set of used filenames
        self.css_font_face_meta = {}  # font_url -> {'family', 'weight', 'style'}
        self.css_background_hints = {}  # img_url -> semantic_hint_str
        self.counters = {
            'img': 0,
            'css': 0,
            'js': 0,
            'fonts': 0,
            'media': 0,
            'data': 0,
            'misc': 0,
        }
        self.network_resources = {}  # url -> {'body': bytes, 'content_type': str}
        self.base_url = url
        def _safe_print(msg):
            try:
                print(msg)
            except UnicodeEncodeError:
                try:
                    print(msg.encode('ascii', errors='backslashreplace').decode('ascii'))
                except Exception:
                    pass

        self.log_callback = log_callback or _safe_print

        self.subdirs = {
            'js':    os.path.join(self.assets_dir, 'js'),
            'css':   os.path.join(self.assets_dir, 'css'),
            'img':   os.path.join(self.assets_dir, 'img'),
            'fonts': os.path.join(self.assets_dir, 'fonts'),
            'media': os.path.join(self.assets_dir, 'media'),
            'data':  os.path.join(self.assets_dir, 'data'),
            'misc':  os.path.join(self.assets_dir, 'misc'),
        }

        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        # Only the root output dir is created upfront.
        # Subdirectories are created lazily in _save_resource.
        os.makedirs(self.assets_dir)

    def log(self, message):
        """Send log message to callback"""
        self.log_callback(message)

    # Reliable MIME→extension table (mimetypes.guess_extension varies by OS)
    _MIME_EXT = {
        'application/javascript':        '.js',
        'text/javascript':               '.js',
        'application/x-javascript':      '.js',
        'module':                         '.js',
        'text/css':                      '.css',
        'text/html':                     '.html',
        'image/svg+xml':                 '.svg',
        'image/webp':                    '.webp',
        'image/jpeg':                    '.jpg',
        'image/png':                     '.png',
        'image/gif':                     '.gif',
        'image/avif':                    '.avif',
        'image/bmp':                     '.bmp',
        'image/tiff':                    '.tif',
        'image/x-icon':                  '.ico',
        'font/woff2':                    '.woff2',
        'font/woff':                     '.woff',
        'font/ttf':                      '.ttf',
        'font/otf':                      '.otf',
        'application/font-woff2':        '.woff2',
        'application/font-woff':         '.woff',
        'application/x-font-ttf':        '.ttf',
        'application/json':              '.json',
        'application/xml':               '.xml',
        'text/xml':                      '.xml',
        'text/plain':                    '.txt',
        'application/octet-stream':      '',
    }

    def _get_extension(self, url, content_type=''):
        """Get file extension from URL or content-type."""
        parsed = urlparse(url)
        _, ext = os.path.splitext(parsed.path)
        if ext and len(ext) <= 6:
            return ext
        if content_type:
            mime = content_type.split(';')[0].strip().lower()
            if mime in self._MIME_EXT:
                return self._MIME_EXT[mime]
            guessed = mimetypes.guess_extension(mime)
            if guessed and len(guessed) <= 6:
                return guessed
        return ''

    def _get_asset_subdir(self, url, content_type=''):
        """Return the assets subdirectory name for a given resource type."""
        ext  = self._get_extension(url, content_type).lower().lstrip('.')
        mime = content_type.split(';')[0].strip().lower() if content_type else ''

        if ext in ('js', 'mjs', 'cjs') or 'javascript' in mime:
            return 'js'
        if ext == 'css' or mime == 'text/css':
            return 'css'
        if ext in ('jpg', 'jpeg', 'jfif', 'pjpeg', 'pjp',
                   'png', 'apng', 'gif', 'webp',
                   'svg', 'svgz',
                   'ico', 'cur',
                   'bmp', 'avif', 'tiff', 'tif') \
                or mime.startswith('image/'):
            return 'img'
        if ext in ('woff', 'woff2', 'ttf', 'otf', 'eot') or 'font' in mime:
            return 'fonts'
        if ext in ('mp4', 'm4v', 'webm', 'ogv', 'avi', 'mov', 'mkv',
                   'mp3', 'm4a', 'wav', 'flac', 'aac', 'oga', 'opus') \
                or mime.startswith(('video/', 'audio/')):
            return 'media'
        if ext in ('json', 'xml', 'csv', 'yaml', 'yml', 'graphql', 'gql') \
                or mime in ('application/json', 'application/xml',
                            'text/xml', 'text/csv', 'application/graphql'):
            return 'data'
        return 'misc'

    @staticmethod
    def _slugify(text, max_len=40):
        if not text:
            return ''
        text = str(text).strip().lower()
        text = re.sub(r'[\s_]+', '-', text)
        text = re.sub(r'[^a-z0-9-]', '', text)
        text = re.sub(r'-+', '-', text).strip('-')
        return text[:max_len].rstrip('-')

    @staticmethod
    def _is_opaque_hash(s):
        if not s:
            return True
        s = s.lower().strip()
        # UUID pattern
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', s):
            return True
        # Hex string length >= 10
        if re.match(r'^[0-9a-f]{10,}$', s):
            return True
        # Base36 / Base64 alphanumeric hash without separators, length >= 14
        if re.match(r'^[a-z0-9]{14,}$', s) and not any(word in s for word in ['icon', 'logo', 'button', 'banner', 'image', 'photo', 'header', 'footer']):
            return True
        # High digit density in length >= 12
        digits = sum(c.isdigit() for c in s)
        if len(s) >= 12 and (digits / len(s) > 0.35) and '-' not in s and '_' not in s:
            return True
        return False

    @staticmethod
    def _read_opentype_font_name(data):
        """Read font family or PostScript name from TTF/OTF/WOFF bytes."""
        if not data or len(data) < 16:
            return None
        try:
            # WOFF 1.0
            if data[:4] == b'wOFF':
                if len(data) < 44:
                    return None
                num_tables = struct.unpack('>H', data[12:14])[0]
                pos = 44
                for _ in range(num_tables):
                    if pos + 20 > len(data):
                        break
                    tag = data[pos:pos+4]
                    offset = struct.unpack('>I', data[pos+4:pos+8])[0]
                    comp_len = struct.unpack('>I', data[pos+8:pos+12])[0]
                    orig_len = struct.unpack('>I', data[pos+12:pos+16])[0]
                    if tag == b'name':
                        if comp_len == orig_len:  # uncompressed
                            return WebsiteDownloader._parse_opentype_name_table(data[offset:offset+comp_len])
                        break
                    pos += 20
                return None

            # TTF / OTF
            tag = data[:4]
            if tag in (b'\x00\x01\x00\x00', b'OTTO', b'true', b'typ1'):
                num_tables = struct.unpack('>H', data[4:6])[0]
                pos = 12
                for _ in range(num_tables):
                    if pos + 16 > len(data):
                        break
                    t_tag = data[pos:pos+4]
                    offset = struct.unpack('>I', data[pos+8:pos+12])[0]
                    length = struct.unpack('>I', data[pos+12:pos+16])[0]
                    if t_tag == b'name':
                        return WebsiteDownloader._parse_opentype_name_table(data[offset:offset+length])
                    pos += 16
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_opentype_name_table(table):
        if not table or len(table) < 6:
            return None
        try:
            count = struct.unpack('>H', table[2:4])[0]
            string_offset = struct.unpack('>H', table[4:6])[0]
            pos = 6
            names = {}
            for _ in range(count):
                if pos + 12 > len(table):
                    break
                p_id, e_id, l_id, n_id, length, offset = struct.unpack('>HHHHHH', table[pos:pos+12])
                pos += 12
                str_start = string_offset + offset
                str_bytes = table[str_start:str_start+length]
                val = None
                if p_id == 3 or (p_id == 0 and e_id != 0):  # Windows / Unicode UTF-16BE
                    try:
                        val = str_bytes.decode('utf-16-be')
                    except Exception:
                        pass
                if not val:
                    try:
                        val = str_bytes.decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                if val and n_id not in names:
                    names[n_id] = val.strip('\x00').strip()
            ps_name = names.get(6)
            family = names.get(1)
            subfamily = names.get(2)
            if ps_name and len(ps_name) < 45 and not WebsiteDownloader._is_opaque_hash(ps_name):
                return ps_name
            if family and subfamily:
                return f"{family}-{subfamily}"
            if family:
                return family
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_cdn_font_name(url):
        parsed = urlparse(url)
        path = parsed.path
        hostname = parsed.netloc.lower()
        if 'gstatic.com' in hostname:
            m = re.search(r'/s/([a-z0-9]+(?:[_-][a-z0-9]+)*)/v', path, re.IGNORECASE)
            if m:
                family = m.group(1).replace('_', '-').title()
                return family
        if 'bunny.net' in hostname:
            m = re.search(r'/([a-z0-9-]+)/files/', path, re.IGNORECASE)
            if m:
                return m.group(1).title()
        if 'typekit.net' in hostname or 'adobe.com' in hostname:
            m = re.search(r'/af/([a-z0-9]+)', path, re.IGNORECASE)
            name = m.group(1) if m else 'typekit'
            return f"adobe-{name}"
        return ''

    def _parse_font_face_rules(self, css_content, css_url):
        """Extract font metadata from @font-face rules in CSS."""
        weight_map = {
            '100': 'Thin',
            '200': 'ExtraLight',
            '300': 'Light',
            '400': 'Regular',
            'normal': 'Regular',
            '500': 'Medium',
            '600': 'SemiBold',
            '700': 'Bold',
            'bold': 'Bold',
            '800': 'ExtraBold',
            '900': 'Black',
        }
        blocks = re.findall(r'@font-face\s*\{([^}]+)\}', css_content, re.IGNORECASE)
        for block in blocks:
            m_fam = re.search(r'font-family\s*:\s*[\'"]?([^\'";]+)[\'"]?', block, re.IGNORECASE)
            family = m_fam.group(1).strip() if m_fam else ''
            
            m_weight = re.search(r'font-weight\s*:\s*([0-9a-zA-Z]+)', block, re.IGNORECASE)
            weight_raw = m_weight.group(1).strip().lower() if m_weight else '400'
            weight_name = weight_map.get(weight_raw, 'Regular')
            
            m_style = re.search(r'font-style\s*:\s*([a-zA-Z]+)', block, re.IGNORECASE)
            style_raw = m_style.group(1).strip().lower() if m_style else 'normal'
            style_name = 'Italic' if style_raw == 'italic' else ''
            
            urls = re.findall(r'url\(\s*[\'"]?([^\'"\)]+)[\'"]?\s*\)', block, re.IGNORECASE)
            for u in urls:
                u_clean = u.split('#')[0].split('?')[0].strip()
                if u_clean and not u_clean.startswith('data:'):
                    abs_u = urljoin(css_url, u_clean)
                    clean_fam = re.sub(r'[^a-zA-Z0-9]', '', family.title())
                    self.css_font_face_meta[abs_u] = {
                        'family': clean_fam or 'Font',
                        'weight': weight_name,
                        'style': style_name,
                    }

    def _extract_css_background_hints(self, css_content, css_url):
        """Extract semantic hints from CSS selectors for background images."""
        rule_pattern = re.compile(r'([^{}]+)\{([^{}]*url\([^)]+\)[^{}]*)\}', re.IGNORECASE)
        for match in rule_pattern.finditer(css_content):
            raw_selector = match.group(1).strip()
            body = match.group(2)
            
            urls = re.findall(r'url\(\s*[\'"]?([^\'"\)]+)[\'"]?\s*\)', body, re.IGNORECASE)
            if not urls:
                continue
                
            classes_ids = re.findall(r'[.#]([a-zA-Z0-9_-]+)', raw_selector)
            best_hint = ''
            for candidate in reversed(classes_ids):
                c_slug = self._slugify(candidate)
                if c_slug in ('lazy-loaded', 'active', 'show', 'hide', 'block', 'flex', 'relative', 'absolute'):
                    continue
                if any(k in c_slug for k in ['bg', 'icon', 'search', 'hero', 'banner', 'logo', 'join', 'footer', 'header', 'card']):
                    best_hint = c_slug
                    break
                if len(c_slug) >= 4 and not best_hint:
                    best_hint = c_slug
                    
            if best_hint:
                best_hint = re.sub(r'^(after-|before-)?(lazy-)?', '', best_hint)
                for u in urls:
                    u_clean = u.split('#')[0].split('?')[0].strip()
                    if u_clean and not u_clean.startswith('data:'):
                        abs_u = urljoin(css_url, u_clean)
                        if abs_u not in self.css_background_hints:
                            self.css_background_hints[abs_u] = best_hint

    def _extract_svg_semantic_name(self, content):
        if not content:
            return ''
        if isinstance(content, bytes):
            content_str = content.decode('utf-8', errors='ignore')
        else:
            content_str = str(content)
            
        m = re.search(r'<svg[^>]*\bid=["\']([^"\']+)["\']', content_str, re.IGNORECASE)
        if m:
            name = self._slugify(m.group(1))
            name = re.sub(r'-(large|small|medium|icon|svg)$', '', name)
            if name and not self._is_opaque_hash(name):
                return name
        m = re.search(r'<svg[^>]*\bdata-name=["\']([^"\']+)["\']', content_str, re.IGNORECASE)
        if m:
            name = self._slugify(m.group(1))
            if name and not self._is_opaque_hash(name):
                return name
        m = re.search(r'<title[^>]*>([^<]+)</title>', content_str, re.IGNORECASE)
        if m:
            name = self._slugify(m.group(1))
            if name and not self._is_opaque_hash(name):
                return name
        m = re.search(r'<symbol[^>]*\bid=["\']([^"\']+)["\']', content_str, re.IGNORECASE)
        if m:
            name = self._slugify(m.group(1))
            if name and len(name) > 1 and not self._is_opaque_hash(name):
                return f"icon-{name}"
        return ''

    def _infer_filename_base(self, url, content, content_type, subdir_name, hint=None):
        """Infer a semantic, clean filename base (without extension) for an asset."""
        parsed = urlparse(url)
        url_path_name = os.path.basename(parsed.path)
        url_stem = os.path.splitext(url_path_name)[0] if url_path_name else ''
        clean_url_stem = self._slugify(url_stem) if not self._is_opaque_hash(url_stem) else ''
        
        # 1. FONTS
        if subdir_name == 'fonts':
            clean_url = url.split('#')[0].split('?')[0]
            if clean_url in self.css_font_face_meta:
                meta = self.css_font_face_meta[clean_url]
                fam = meta['family']
                w = meta['weight']
                st = meta['style']
                variant = f"{w}{st}".strip()
                if fam:
                    return f"{fam}-{variant}" if variant and variant != 'Regular' else fam
                    
            ot_name = self._read_opentype_font_name(content)
            if ot_name:
                slug_ot = re.sub(r'[^a-zA-Z0-9_-]', '', ot_name)
                if slug_ot and not self._is_opaque_hash(slug_ot):
                    return slug_ot
                    
            cdn_font = self._parse_cdn_font_name(url)
            if cdn_font:
                return cdn_font
                
            if clean_url_stem and len(clean_url_stem) >= 3:
                return clean_url_stem
                
            self.counters['fonts'] += 1
            return f"font-{self.counters['fonts']}"

        # 2. IMAGES
        if subdir_name == 'img':
            if hint == 'favicon' or (hint and 'icon' in str(hint) and ('.ico' in url or 'icon' in url)):
                return 'favicon'
                
            is_svg = url.endswith('.svg') or 'svg' in content_type or (content and content[:200].lstrip().startswith(b'<svg'))
            if is_svg:
                svg_name = self._extract_svg_semantic_name(content)
                if svg_name:
                    return svg_name
                    
            if hint and isinstance(hint, str) and hint != 'favicon':
                slug_hint = self._slugify(hint)
                if slug_hint and not self._is_opaque_hash(slug_hint):
                    return slug_hint
                    
            clean_url = url.split('#')[0].split('?')[0]
            if clean_url in self.css_background_hints:
                bg_hint = self.css_background_hints[clean_url]
                if bg_hint and not self._is_opaque_hash(bg_hint):
                    return bg_hint
                    
            if clean_url_stem and len(clean_url_stem) >= 3:
                return clean_url_stem
                
            self.counters['img'] += 1
            prefix = 'icon' if is_svg else 'img'
            return f"{prefix}-{self.counters['img']}"

        # 3. CSS
        if subdir_name == 'css':
            if hint and isinstance(hint, str):
                slug_hint = self._slugify(hint)
                if slug_hint:
                    return slug_hint
            if clean_url_stem and any(fw in clean_url_stem for fw in ['bootstrap', 'tailwind', 'fontawesome', 'animate', 'style', 'main', 'theme']):
                return clean_url_stem
            self.counters['css'] += 1
            if self.counters['css'] == 1:
                return 'style'
            return f"style-{self.counters['css']}"

        # 4. JAVASCRIPT
        if subdir_name == 'js':
            if hint and isinstance(hint, str):
                slug_hint = self._slugify(hint)
                if slug_hint:
                    return slug_hint
                    
            if content:
                try:
                    head_text = content[:1500].decode('utf-8', errors='ignore')
                    m_lic = re.search(r'([a-zA-Z0-9_-]+)\.js\.LICENSE', head_text)
                    if m_lic:
                        name = self._slugify(m_lic.group(1))
                        if name and not self._is_opaque_hash(name):
                            return name
                    m_lib = re.search(r'/\*!\s*([a-zA-Z0-9._-]+)\s+(?:v[0-9]|bundle|library)', head_text, re.IGNORECASE)
                    if m_lib:
                        name = self._slugify(m_lib.group(1))
                        if name:
                            return name
                except Exception:
                    pass
                    
            if clean_url_stem and any(lib in clean_url_stem for lib in ['app', 'main', 'bundle', 'vendor', 'runtime', 'polyfills', 'index']):
                return clean_url_stem
                
            self.counters['js'] += 1
            if self.counters['js'] == 1:
                return 'app'
            return f"script-{self.counters['js']}"

        if clean_url_stem and len(clean_url_stem) >= 3:
            return clean_url_stem
        self.counters[subdir_name] = self.counters.get(subdir_name, 0) + 1
        return f"{subdir_name}-{self.counters[subdir_name]}"

    def _save_resource(self, url, content, content_type='', hint=None):
        """Save a resource to the appropriate typed subdirectory with deduplication and clean naming."""
        if not content:
            return None

        if url in self.resource_cache:
            return self.resource_cache[url]

        content_bytes = content if isinstance(content, bytes) else content.encode('utf-8')

        # Deduplication by content hash
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        if content_hash in self.content_cache:
            existing_path = self.content_cache[content_hash]
            self.resource_cache[url] = existing_path
            return existing_path

        subdir_name = self._get_asset_subdir(url, content_type)
        subdir_path = self.subdirs[subdir_name]
        ext = self._get_extension(url, content_type)

        base_name = self._infer_filename_base(url, content_bytes, content_type, subdir_name, hint)

        if not base_name.lower().endswith(ext.lower()):
            base_name = f"{base_name}{ext}"

        if subdir_name not in self.used_names:
            self.used_names[subdir_name] = set()

        # Clean collision resolution (-2, -3) without hashes
        final_name = base_name
        counter = 2
        root, f_ext = os.path.splitext(base_name)
        while final_name in self.used_names[subdir_name]:
            final_name = f"{root}-{counter}{f_ext}"
            counter += 1

        self.used_names[subdir_name].add(final_name)

        filepath = os.path.join(subdir_path, final_name)
        os.makedirs(subdir_path, exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(content_bytes)

        rel_path = f"assets/{subdir_name}/{final_name}"
        self.resource_cache[url] = rel_path
        self.content_cache[content_hash] = rel_path
        return rel_path

    def _download_fallback(self, url, hint=None):
        """Download a resource that wasn't captured during page load"""
        if url in self.resource_cache:
            return self.resource_cache[url]
        
        if not url or url.startswith(('data:', 'blob:', '#')):
            return url
            
        try:
            response = self.session.get(url, timeout=15, verify=False)
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                local_path = self._save_resource(url, response.content, content_type, hint=hint)
                return local_path
        except Exception:
            pass  # Silent fail for fallback
        
        return None

    def _get_resource(self, url, base=None, hint=None):
        """Get a resource - from cache, network capture, or fallback download"""
        if not url or url.startswith(('data:', 'blob:', '#')):
            return url
        
        # Make absolute URL
        abs_url = urljoin(base or self.base_url, url)
        
        # Check cache first
        if abs_url in self.resource_cache:
            return self.resource_cache[abs_url]
        
        # Check network captures
        if abs_url in self.network_resources:
            res = self.network_resources[abs_url]
            return self._save_resource(abs_url, res['body'], res.get('content_type', ''), hint=hint)
        
        # Fallback download
        local_path = self._download_fallback(abs_url, hint=hint)
        if local_path:
            return local_path
        
        # Return original if all fails
        return url

    def _rewrite_css_urls(self, css_content, css_url, css_local_path=None):
        """Rewrite all url() references in CSS content preserving fragments.

        css_local_path: relative path of the CSS file from output_dir root
                        (e.g. 'assets/css/main.css'). Pass '' for inline styles.
        """
        css_dir = os.path.dirname(css_local_path) if css_local_path else ''

        def replacer(match):
            full_match = match.group(0)
            url_content = match.group(1).strip()

            if url_content.startswith(("'", '"')) and url_content.endswith(("'", '"')):
                url_content = url_content[1:-1]

            if not url_content or url_content.startswith('data:'):
                return full_match

            # Separate fragment if present (e.g. font.woff2#iefix or sprite.svg#icon)
            fragment = ''
            clean_url = url_content
            if '#' in url_content:
                clean_url, fragment = url_content.split('#', 1)
                fragment = f"#{fragment}"

            abs_url = urljoin(css_url, clean_url)
            local_path = self._get_resource(abs_url)

            if local_path and local_path.startswith('assets/'):
                rel = os.path.relpath(local_path, css_dir).replace(os.sep, '/') if css_dir else local_path
                return f'url("{rel}{fragment}")'

            return full_match

        return re.sub(r'url\(\s*([^)]+)\s*\)', replacer, css_content)

    def _detect_nextjs(self, soup):
        """Detect if page is built with Next.js even without #__next"""
        # Check for Next.js data script
        for script in soup.find_all('script'):
            script_id = script.get('id', '')
            script_text = script.string or ''
            if '__NEXT_DATA__' in script_id or '__NEXT_DATA__' in script_text:
                return True
            if 'self.__next' in script_text:
                return True
        
        # Check for Next.js script patterns in src
        for script in soup.find_all('script', src=True):
            src = script['src']
            if '_next/' in src or 'webpack' in src.lower():
                return True
        
        # Check for Next.js link patterns
        for link in soup.find_all('link'):
            href = link.get('href', '')
            if '_next/' in href:
                return True
        
        return False

    def _fix_scroll_blocking(self, soup):
        """Fix CSS and HTML issues that block scrolling in offline viewing"""
        self.log("🔧 Corrigindo problemas de scroll para visualização offline...")
        
        # 1. Fix html element
        html_elem = soup.find('html')
        if html_elem:
            html_classes = html_elem.get('class', [])
            if isinstance(html_classes, str):
                html_classes = html_classes.split()
            
            # Remove Lenis-specific classes that block scroll
            lenis_classes = ['lenis', 'lenis-smooth', 'lenis-scrolling', 'lenis-stopped', 
                           'has-scroll-smooth', 'has-scroll-init', 'locomotive-scroll']
            new_classes = [c for c in html_classes if c.lower() not in [lc.lower() for lc in lenis_classes]]
            if new_classes != html_classes:
                html_elem['class'] = new_classes
                self.log("   ✅ Removidas classes Lenis/Locomotive do html")
        
        # 2. Fix body element
        body = soup.find('body')
        if body:
            body_classes = body.get('class', [])
            if isinstance(body_classes, str):
                body_classes = body_classes.split()
            
            # Remove scroll-blocking classes
            blocking_classes = ['overflow-hidden', 'no-scroll', 'scroll-lock', 'fixed', 
                              'lenis', 'lenis-smooth', 'has-scroll-smooth']
            new_classes = [c for c in body_classes if c.lower() not in [bc.lower() for bc in blocking_classes]]
            
            # Fix flex centering that cuts off content
            if 'items-center' in new_classes and 'flex' in new_classes:
                new_classes = [c if c != 'items-center' else 'items-start' for c in new_classes]
                self.log("   ✅ Corrigida centralização vertical do body")
            
            if new_classes != body_classes:
                body['class'] = new_classes
        
        # 3. Fix main containers that might have height: 100vh with overflow hidden
        problematic_selectors = [
            '[data-scroll-container]',
            '.scroll-container', 
            '.smooth-scroll',
            '[data-lenis-prevent]',
            '.lenis-wrapper',
        ]
        
        for elem in soup.find_all(class_=lambda c: c and any(
            x in str(c).lower() for x in ['scroll-container', 'smooth-scroll', 'lenis', 'locomotive']
        )):
            # Remove data attributes that control smooth scroll
            for attr in list(elem.attrs.keys()):
                if 'scroll' in attr.lower() or 'lenis' in attr.lower():
                    del elem[attr]
        
        # 4. Remove/fix inline styles that block scroll
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'overflow' in style.lower() and 'hidden' in style.lower():
                # Remove overflow: hidden from inline styles
                new_style = re.sub(r'overflow\s*:\s*hidden\s*;?', '', style, flags=re.IGNORECASE)
                elem['style'] = new_style.strip()
        
        # 5. Inject CSS overrides to ensure scrolling works
        scroll_fix_css = """
        /* Scroll fixes for offline viewing */
        html, body {
            overflow: auto !important;
            overflow-x: hidden !important;
            height: auto !important;
            min-height: 100% !important;
            scroll-behavior: auto !important;
            opacity: 1 !important;
            visibility: visible !important;
        }

        /* Force visibility of main wrappers */
        body, .wrapper, main, #__next, #app, .page, .content {
            opacity: 1 !important;
            visibility: visible !important;
        }

        /* Hide loader/preloader overlays only */
        .loader, .preloader, .loading,
        [class*="loader"], [class*="preloader"],
        #loader, #preloader, #loading, .page-loader, .site-loader {
            display: none !important;
        }

        /* Lenis / Locomotive Scroll containers */
        html.lenis, html.lenis-smooth,
        body.lenis, body.lenis-smooth,
        .lenis-wrapper, .lenis-content,
        [data-lenis-prevent], [data-scroll-container] {
            overflow: visible !important;
            height: auto !important;
        }

        /* Fix body flex centering that clips content */
        body.flex.items-center, body.flex.justify-center {
            align-items: flex-start !important;
            min-height: 100vh;
            height: auto !important;
        }

        /* Ensure main content scrolls */
        main, #__next, #__nuxt, #app, .main-content {
            overflow: visible !important;
            height: auto !important;
        }
        """
        
        # Add the fix CSS as a style tag at the end of head
        head = soup.find('head')
        if head:
            fix_style = soup.new_tag('style')
            fix_style['data-scroll-fix'] = 'true'
            fix_style.string = scroll_fix_css
            head.append(fix_style)
            self.log("   ✅ Injetado CSS para corrigir scroll")
        
        # 6. Remove Lenis/Locomotive script tags that might interfere
        scripts_removed = 0
        for script in soup.find_all('script'):
            src = script.get('src', '') or ''
            script_text = script.string or ''
            
            # Check for smooth scroll libraries
            if any(x in src.lower() for x in ['lenis', 'locomotive', 'smooth-scroll']):
                script.decompose()
                scripts_removed += 1
            elif any(x in script_text.lower() for x in ['new lenis', 'new locomotivescroll', 'smoothscroll']):
                script.decompose()
                scripts_removed += 1
        
        if scripts_removed > 0:
            self.log(f"   ✅ Removidos {scripts_removed} scripts de smooth scroll")

    def _process_srcset(self, srcset, base=None, hint=None):
        """Process a srcset attribute and return the rewritten version"""
        if not srcset:
            return srcset
        
        new_parts = []
        parts = srcset.split(',')
        
        for part in parts:
            part = part.strip()
            if not part:
                continue
            
            tokens = part.split()
            if not tokens:
                continue
            
            url = tokens[0]
            descriptor = ' '.join(tokens[1:]) if len(tokens) > 1 else ''
            
            if url.startswith('data:'):
                new_parts.append(part)
                continue
            
            local_path = self._get_resource(url, base, hint=hint)
            if local_path and local_path != url:
                new_parts.append(f"{local_path} {descriptor}".strip())
            else:
                new_parts.append(part)
        
        return ', '.join(new_parts) if new_parts else srcset

    def _extract_iframe_content(self, page):
        """
        Check if the page content is inside an iframe (common in site builders like Aura, Webflow, etc.)
        and extract the actual content if found.
        """
        # Check for srcdoc iframes (content embedded in attribute)
        srcdoc_iframe = page.query_selector('iframe[srcdoc]')
        if srcdoc_iframe:
            self.log("🔍 Detectado iframe com srcdoc - extraindo conteúdo real...")
            srcdoc = srcdoc_iframe.get_attribute('srcdoc')
            if srcdoc:
                # Decode HTML entities
                decoded_content = html.unescape(srcdoc)
                return decoded_content, True
        
        # Check for preview frames (common in site builders)
        preview_selectors = [
            'iframe[class*="preview"]',
            'iframe[class*="site-frame"]',
            'iframe[class*="canvas"]',
            'iframe[id*="preview"]',
            '#preview-iframe',
            '.preview-frame iframe',
            '[role="tabpanel"] iframe',  # Aura-style tab panels
            '[data-testid*="preview"] iframe',
        ]
        
        for selector in preview_selectors:
            iframe = page.query_selector(selector)
            if iframe:
                # Try to get the frame content
                frames = page.frames
                for frame in frames:
                    if frame != page.main_frame and frame.url and frame.url != 'about:blank':
                        try:
                            self.log(f"🔍 Detectado iframe de preview - extraindo de {frame.url[:50]}...")
                            content = frame.content()
                            if len(content) > 500:  # Has substantial content
                                self.base_url = frame.url
                                return content, True
                        except:
                            pass
        
        # Check all frames including those with srcdoc (about:srcdoc URL)
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    frame_url = frame.url
                    # Handle frames with srcdoc (they have about:srcdoc URL)
                    if frame_url == 'about:srcdoc':
                        content = frame.content()
                        if len(content) > 1000:  # Substantial content
                            self.log("🔍 Detectado iframe srcdoc via frame - extraindo conteúdo...")
                            return content, True
                except:
                    pass
        
        # Check if main content is suspiciously small (might be a wrapper)
        main_content = page.content()
        body = page.query_selector('body')
        if body:
            # Check if body has very few elements but contains an iframe
            direct_children = page.query_selector_all('body > *')
            iframes = page.query_selector_all('iframe')
            
            if len(direct_children) <= 5 and len(iframes) > 0:
                # Page might be a wrapper - try to get iframe content
                for frame in page.frames:
                    if frame != page.main_frame:
                        try:
                            content = frame.content()
                            if len(content) > len(main_content) * 0.3:
                                self.log("🔍 Detectado wrapper com iframe - usando conteúdo do frame...")
                                if frame.url and frame.url not in ['about:blank', 'about:srcdoc']:
                                    self.base_url = frame.url
                                return content, True
                        except:
                            pass
        
        return None, False

    def process(self):
        with sync_playwright() as p:
            self.log("🚀 Iniciando navegador...")
            # Launch with reduced memory footprint
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-extensions',
                    '--disable-default-apps',
                    '--disable-sync',
                    '--disable-translate',
                    '--metrics-recording-only',
                    '--mute-audio',
                    '--no-first-run',
                    '--safebrowsing-disable-auto-update',
                ]
            )
            
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
            )
            
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            # Capture network responses (including redirects)
            def capture_response(response):
                try:
                    url = response.url
                    if response.status == 200 and not url.startswith(('data:', 'blob:')):
                        url_lower = url.lower()
                        # Skip capturing junk tracking / captcha scripts into memory
                        if any(j in url_lower for j in WebsiteDownloader.JUNK_DOMAINS):
                            return
                        try:
                            body = response.body()
                            # Skip excessively large payloads (> 15 MB) to avoid memory spikes
                            if len(body) > 15 * 1024 * 1024:
                                return
                            resource_data = {
                                'body': body,
                                'content_type': response.headers.get('content-type', '')
                            }
                            # Store by final URL
                            self.network_resources[url] = resource_data
                            
                            # Also store by original request URL (handles redirects)
                            request_url = response.request.url
                            if request_url != url:
                                self.network_resources[request_url] = resource_data
                        except:
                            pass
                except:
                    pass
            
            page.on("response", capture_response)
            
            self.log(f"🌐 Carregando {self.url}...")
            try:
                page.goto(self.url, wait_until='load', timeout=60000)
                self.log("✓ Página carregada (load)")
                try:
                    page.wait_for_load_state('networkidle', timeout=5000)
                    self.log("✓ Rede ociosa — recursos adicionais prontos")
                except Exception:
                    self.log("⚠️ Rede ainda ativa após 5 s, continuando mesmo assim...")
            except Exception as e:
                self.log(f"⚠️ Aviso de carregamento: {str(e)[:100]}")
                self.log("⚠️ Tentando continuar mesmo assim...")

            self.base_url = page.url

            # Check for iframe content (site builders like Aura, Webflow, etc.)
            iframe_content, is_iframe = self._extract_iframe_content(page)

            if not is_iframe:
                self.log("📜 Rolando página para carregar conteúdo lazy...")
                self._scroll_page(page)
                try:
                    page.wait_for_load_state('networkidle', timeout=3000)
                except Exception:
                    pass
                self._force_reveal_animations(page)

            # Get cookies from browser for fallback downloads
            cookies = context.cookies()

            # Setup requests session with browser cookies
            self.session = requests.Session()
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': self.base_url,
            })
            for cookie in cookies:
                self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))

            # Get final HTML - use iframe content if detected
            if is_iframe and iframe_content:
                html_content = iframe_content
                self.log("✨ Usando conteúdo extraído do iframe")
            else:
                html_content = page.content()

            # Pre-download background images visible only via computed styles
            self._capture_computed_backgrounds(page)

            self.log(f"📦 Capturados {len(self.network_resources)} recursos de rede")

            browser.close()
        
        # Process HTML
        self.log("🔧 Processando HTML e assets...")
        soup = BeautifulSoup(html_content, 'html.parser')

        # 0. Handle <base> tag to avoid resolving offline assets against remote origin
        base_tag = soup.find('base', href=True)
        if base_tag:
            self.base_url = urljoin(self.base_url, base_tag['href'])
            base_tag.decompose()
            self.log("   ✅ Tag <base> resolvida e removida para funcionamento offline")

        # 0.1 Remove platform-specific security/firewall scripts
        self._remove_platform_scripts(soup)

        # 0.2 Remove tracking, analytics, and captcha scripts
        self._remove_junk_scripts(soup)

        # 0.3 Remove SPA framework scripts BEFORE downloading them
        self._remove_framework_scripts(soup)

        # 0.4 Fix scroll-blocking issues for offline viewing
        self._fix_scroll_blocking(soup)

        # Remove any remaining iframes that are wrappers (like Aura preview frames)
        for iframe in soup.find_all('iframe'):
            src = iframe.get('src', '') or ''
            srcdoc = iframe.get('srcdoc', '')
            if srcdoc or 'preview' in str(iframe.get('class', '')).lower():
                iframe.decompose()

        # 1. Process external stylesheets
        self.log("🎨 Processando stylesheets...")
        css_idx = 0
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href')
            if not href or href.startswith('data:'):
                continue
            
            abs_url = urljoin(self.base_url, href)
            
            css_content = None
            if abs_url in self.network_resources:
                try:
                    css_content = self.network_resources[abs_url]['body'].decode('utf-8', errors='ignore')
                except Exception:
                    pass
            
            if not css_content and self.session:
                try:
                    response = self.session.get(abs_url, timeout=15, verify=False)
                    if response.status_code == 200:
                        css_content = response.text
                except Exception:
                    pass
            
            if css_content:
                css_idx += 1
                hint = link.get('id') or link.get('title') or ('style' if css_idx == 1 else f"style-{css_idx}")
                
                # Inlining imports and extracting font-face / background hints
                css_content = self._inline_css_imports(css_content, abs_url)
                self._parse_font_face_rules(css_content, abs_url)
                self._extract_css_background_hints(css_content, abs_url)
                
                css_content = self._rewrite_css_urls(css_content, abs_url, css_local_path='assets/css/style.css')
                local_path = self._save_resource(abs_url, css_content.encode('utf-8'), 'text/css', hint=hint)
                if local_path:
                    link['href'] = local_path

        # 2. Process inline <style> tags
        self.log("✨ Processando estilos inline...")
        for style_tag in soup.find_all('style'):
            if style_tag.string:
                self._parse_font_face_rules(style_tag.string, self.base_url)
                self._extract_css_background_hints(style_tag.string, self.base_url)
                style_tag.string = self._rewrite_css_urls(style_tag.string, self.base_url, css_local_path='')

        # 3. Process scripts
        self.log("📝 Processando scripts...")
        for script in soup.find_all('script', src=True):
            src = script.get('src')
            if not src or src.startswith('data:'):
                continue

            # Skip junk scripts if any survived
            if any(j in src.lower() for j in self.JUNK_DOMAINS):
                script.decompose()
                continue

            hint = script.get('id') or script.get('data-component')
            local_path = self._get_resource(src, hint=hint)
            if local_path and local_path != src:
                script['src'] = local_path
                for attr in ['integrity', 'crossorigin', 'nonce']:
                    if script.has_attr(attr):
                        del script[attr]

        # 4. Process all image-related elements
        self.log("🖼️ Processando imagens...")
        for elem in soup.find_all(['img', 'source', 'video', 'audio', 'picture', 'input']):
            # Extract semantic hint from HTML attributes
            hint = (
                elem.get('alt') or
                elem.get('data-test-id') or
                elem.get('aria-label') or
                elem.get('title') or
                elem.get('id') or
                ''
            )
            if not hint and elem.get('class'):
                classes = elem.get('class')
                if isinstance(classes, list):
                    classes = ' '.join(classes)
                for candidate in classes.split():
                    if any(w in candidate.lower() for w in ['logo', 'avatar', 'hero', 'banner', 'illustration', 'thumb']):
                        hint = candidate
                        break

            # Process src
            src = elem.get('src')
            
            # Check lazy loading attributes first
            for attr in ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-image', 'data-bg']:
                if elem.get(attr):
                    lazy_src = elem[attr]
                    local_path = self._get_resource(lazy_src, hint=hint)
                    if local_path and local_path != lazy_src:
                        elem['src'] = local_path
                        del elem[attr]
                        src = None  # Already handled
                    break
            
            if src and not src.startswith('data:'):
                local_path = self._get_resource(src, hint=hint)
                if local_path and local_path != src:
                    elem['src'] = local_path
            
            # Process srcset
            srcset = elem.get('srcset')
            if srcset:
                elem['srcset'] = self._process_srcset(srcset, hint=hint)
            
            # Process data-srcset
            data_srcset = elem.get('data-srcset')
            if data_srcset:
                elem['data-srcset'] = self._process_srcset(data_srcset, hint=hint)
            
            # Process poster for video
            if elem.name == 'video' and elem.get('poster'):
                poster = elem['poster']
                local_path = self._get_resource(poster, hint='video-poster')
                if local_path and local_path != poster:
                    elem['poster'] = local_path

        # 4.1 Process SVG <use> tags
        for use_tag in soup.find_all('use'):
            for attr in ['href', 'xlink:href']:
                val = use_tag.get(attr)
                if val:
                    if '#' in val:
                        svg_url, frag = val.split('#', 1)
                        if svg_url:
                            local_path = self._get_resource(svg_url, hint='icon-sprite')
                            if local_path and local_path != svg_url:
                                use_tag[attr] = f"{local_path}#{frag}"
                    elif not val.startswith(('data:', '#')):
                        local_path = self._get_resource(val, hint='icon-sprite')
                        if local_path and local_path != val:
                            use_tag[attr] = local_path

        # 5. Process inline style attributes
        self.log("🔗 Processando atributos de estilo inline...")
        for elem in soup.find_all(attrs={'style': True}):
            style = elem['style']
            if 'url(' in style:
                elem['style'] = self._rewrite_css_urls(style, self.base_url, css_local_path='')

        # 6. Process favicons and other link tags with URLs
        for link in soup.find_all('link'):
            if link.get('href') and link.get('rel'):
                rel = link['rel']
                if isinstance(rel, list):
                    rel = ' '.join(rel)
                if any(x in rel.lower() for x in ['icon', 'apple-touch', 'manifest']):
                    href = link['href']
                    if not href.startswith('data:'):
                        hint = 'apple-touch-icon' if 'apple-touch' in rel.lower() else 'favicon'
                        local_path = self._get_resource(href, hint=hint)
                        if local_path and local_path != href:
                            link['href'] = local_path

        # 7. Process meta tags with image URLs (og:image, etc.)
        for meta in soup.find_all('meta', attrs={'content': True}):
            prop = meta.get('property', '') or meta.get('name', '')
            if 'image' in prop.lower():
                content = meta['content']
                if content and not content.startswith('data:') and ('http' in content or content.startswith('/')):
                    local_path = self._get_resource(content, hint='og-image')
                    if local_path and local_path != content:
                        meta['content'] = local_path

        # 8. Process background images in divs and other elements
        for elem in soup.find_all(attrs={'data-background': True}):
            bg = elem['data-background']
            if bg and not bg.startswith('data:'):
                local_path = self._get_resource(bg, hint='background')
                if local_path and local_path != bg:
                    elem['data-background'] = local_path

        # 9. Fix navigation links that won't work locally
        self.log("🔗 Corrigindo links de navegação...")
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Preserve in-page anchor navigation (e.g. "/#features" -> "#features")
            if '/#' in href:
                a['href'] = href[href.index('/#'):].replace('/#', '#')
            elif href == '/' or href == '':
                a['href'] = '#'
            elif href.startswith('/') and not href.startswith('//'):
                if '#' in href:
                    a['href'] = href[href.index('#'):]
                else:
                    a['href'] = '#'

        # 10. Post-process: fallback download for any remaining external img/source URLs
        self.log("🔍 Verificando imagens externas não capturadas...")
        external_captured = 0
        for elem in soup.find_all(['img', 'source']):
            hint = elem.get('alt') or elem.get('title') or ''
            for attr in ['src', 'data-src']:
                val = elem.get(attr, '')
                if val and (val.startswith('http') or val.startswith('//')):
                    target_url = val if not val.startswith('//') else 'https:' + val
                    local_path = self._download_fallback(target_url, hint=hint)
                    if local_path:
                        elem[attr] = local_path
                        external_captured += 1
        if external_captured:
            self.log(f"   ✅ {external_captured} imagem(ns) externas capturadas no pós-processamento")

        # Save HTML — ensure DOCTYPE is present for standards mode
        html_output = str(soup)
        stripped = html_output.lstrip()
        if not (stripped.lower().startswith('<!doctype')):
            html_output = '<!DOCTYPE html>\n' + html_output
            self.log("✅ <!DOCTYPE html> adicionado")

        with open(os.path.join(self.output_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(html_output)

        # Build a per-folder summary from the resource cache
        folder_counts: dict[str, int] = {}
        for rel_path in self.resource_cache.values():
            parts = rel_path.split('/')       # e.g. ['assets', 'img', 'logo.png']
            folder = parts[1] if len(parts) >= 3 else 'misc'
            folder_counts[folder] = folder_counts.get(folder, 0) + 1

        self.log(f"✅ Concluído! {len(self.resource_cache)} assets salvos e organizados:")
        icons = {'js': '📜', 'css': '🎨', 'img': '🖼️', 'fonts': '🔤', 'media': '🎬', 'data': '📊', 'misc': '📦'}
        for folder in ('css', 'js', 'img', 'fonts', 'media', 'data', 'misc'):
            count = folder_counts.get(folder, 0)
            if count:
                self.log(f"   {icons.get(folder, '📁')} assets/{folder}/  → {count} arquivo(s)")
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Platform script removal
    # ─────────────────────────────────────────────────────────────────────────
    _PLATFORM_SCRIPT_IDS = {
        'aura-supabase-token-firewall',
        'aura-referral-tracking',
        'aura-ga4-start',
        '__webflow_edge_middleware',
        'wix-warmup-data',
        'squarespace-inline-scripts',
    }
    _PLATFORM_SCRIPT_PATTERNS = [
        '__AURA_SUPABASE_FIREWALL__',
        'patchStorage',
        '__WEBFLOW_',
        'webflow.com/api',
        'wixBiSession',
        'squarespace-platform',
    ]

    def _remove_platform_scripts(self, soup):
        """Remove platform-specific security/firewall/tracking scripts that
        intercept fetch/XHR/cookies and break offline functionality."""
        removed = 0
        for script in soup.find_all('script'):
            sid = script.get('id', '')
            src = script.get('src', '') or ''
            text = script.string or ''
            if sid in self._PLATFORM_SCRIPT_IDS:
                script.decompose(); removed += 1; continue
            if any(p in text for p in self._PLATFORM_SCRIPT_PATTERNS):
                script.decompose(); removed += 1; continue
            # Remove scripts whose src/text patches core browser APIs
            if 'patchFetch' in text or 'patchXHR' in text or 'patchWebSocket' in text:
                script.decompose(); removed += 1
        if removed:
            self.log(f"🔒 Removidos {removed} script(s) de plataforma (firewall/tracking)")

    def _remove_junk_scripts(self, soup):
        """Remove tracking, analytics, ads, beacons and captchas that fail offline and bloat assets."""
        removed = 0
        for script in soup.find_all('script'):
            src = (script.get('src') or '').lower()
            text = (script.get_text() or '').lower()
            
            is_junk = False
            if src:
                for domain in self.JUNK_DOMAINS:
                    if domain in src:
                        is_junk = True
                        break
                if not is_junk:
                    for pattern in self.JUNK_SRC_PATTERNS:
                        if re.search(pattern, src, re.IGNORECASE):
                            is_junk = True
                            break
                            
            if not is_junk and text:
                if any(sig in text for sig in [
                    'googletagmanager.com/gtm.js',
                    'google-analytics.com/analytics.js',
                    'www.google-analytics.com/gtag/js',
                    'fbevents.js',
                    'connect.facebook.net',
                    'clarity.ms/tag',
                    'static.hotjar.com',
                    'utag_data',
                    '__tealium',
                    'newrelic',
                    'browser-agent.datadoghq',
                    'grecaptcha.execute',
                    'grecaptcha.ready',
                ]):
                    is_junk = True
                    
            if is_junk:
                script.decompose()
                removed += 1
                
        if removed:
            self.log(f"🧹 Removidos {removed} script(s) de rastreamento, analytics e captcha")

    def _remove_framework_scripts(self, soup):
        """Remove SPA hydration scripts (Next.js, Nuxt, Gatsby) BEFORE downloading them."""
        is_gatsby = soup.find(id='___gatsby') is not None
        is_nextjs = soup.find(id='__next') is not None or self._detect_nextjs(soup)
        is_nuxt = soup.find(id='__nuxt') is not None

        if not (is_gatsby or is_nextjs or is_nuxt):
            return

        framework = 'Gatsby' if is_gatsby else ('Next.js' if is_nextjs else 'Nuxt')
        self.log(f"🛡️ Detectado {framework} - removendo scripts de hidratação do framework...")

        scripts_removed = 0
        for script in soup.find_all('script'):
            src = script.get('src', '')
            script_text = script.get_text() or ''

            should_remove = False

            if is_gatsby and ('framework-' in src or 'app-' in src or 
                             'commons-' in src or 'component-' in src or
                             'webpack-runtime' in src or 'polyfill' in src):
                should_remove = True

            if is_nextjs:
                if '_next/' in src or 'webpack' in src or 'polyfill' in src:
                    should_remove = True
                if '__next' in script_text or 'self.__next' in script_text or '__NEXT_DATA__' in script_text:
                    should_remove = True

            if is_nuxt and ('_nuxt/' in src or '__NUXT__' in script_text or 'nuxt' in src.lower()):
                should_remove = True

            if ('hydrate' in script_text.lower() or 
                'window.__' in script_text or
                'pageData' in script_text or
                '__NEXT_DATA__' in script_text):
                should_remove = True

            if should_remove:
                script.decompose()
                scripts_removed += 1

        links_removed = 0
        for link in soup.find_all('link', rel=lambda r: r and any(x in r for x in ['preload', 'prefetch', 'modulepreload'])):
            href = link.get('href', '')
            if '_next/' in href or '_nuxt/' in href:
                link.decompose()
                links_removed += 1

        if scripts_removed or links_removed:
            self.log(f"   ✅ Removidos {scripts_removed} scripts e {links_removed} preloads de {framework}")

    # ─────────────────────────────────────────────────────────────────────────
    # Computed background capture (called while browser is still open)
    # ─────────────────────────────────────────────────────────────────────────
    def _capture_computed_backgrounds(self, page):
        """Find elements whose background-image is only visible in computed styles
        (set by JS or CSS classes, not in HTML) and pre-download them so the
        _rewrite_css_urls step can later localise them."""
        try:
            bg_urls = page.evaluate(r"""
            () => {
                const urls = new Set();
                document.querySelectorAll('*').forEach(el => {
                    const bg = window.getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none') {
                        const m = bg.match(/url\(["']?([^"'()]+)["']?\)/);
                        if (m && m[1] && !m[1].startsWith('data:')) urls.add(m[1]);
                    }
                });
                return [...urls];
            }
            """)
            pre = 0
            for url in (bg_urls or []):
                if url and not url.startswith('data:'):
                    abs_url = urljoin(self.base_url, url)
                    if abs_url not in self.network_resources:
                        self._download_fallback(abs_url)
                        pre += 1
            if pre:
                self.log(f"🖼️ {pre} background-image(s) pré-capturado(s) via computed styles")
        except Exception as e:
            self.log(f"⚠️ Aviso ao capturar computed backgrounds: {e}")

    def _force_reveal_animations(self, page):
        """Force JS-driven animations (AOS, GSAP, Framer Motion, ScrollReveal) to
        their final visible state so the captured HTML shows fully revealed content.
        Pure CSS @keyframes are NOT touched — they will play normally offline.
        """
        try:
            page.evaluate(r"""
            () => {
                // ── AOS ──────────────────────────────────────────────────────────
                document.querySelectorAll('[data-aos]').forEach(el => {
                    el.classList.add('aos-animate');
                    el.style.transitionDuration = '0s';
                    el.style.transitionDelay = '0s';
                });
                if (window.AOS) {
                    try { AOS.refreshHard(); } catch(e) {}
                }

                // ── GSAP ScrollTrigger ────────────────────────────────────────────
                if (window.ScrollTrigger) {
                    try {
                        ScrollTrigger.getAll().forEach(t => t.progress(1, false));
                        ScrollTrigger.refresh();
                    } catch(e) {}
                }

                // ── WOW.js / Animate.css ──────────────────────────────────────────
                document.querySelectorAll('.wow').forEach(el => {
                    el.classList.add('animated');
                    el.style.animationDelay = '0s';
                    el.style.visibility = 'visible';
                });

                // ── Locomotive / Lenis in-view classes ────────────────────────────
                document.querySelectorAll('[data-scroll], [data-inview], [data-animate], [data-reveal]').forEach(el => {
                    el.classList.add('is-inview', 'in-view', 'revealed');
                });

                // ── ScrollReveal ──────────────────────────────────────────────────
                document.querySelectorAll('[data-sr-id], .sr').forEach(el => {
                    el.style.visibility = 'visible';
                    el.style.opacity = '1';
                });

                // ── Elements stuck with inline opacity:0 / translateY after JS init ──
                // Only reset elements that STILL have opacity:0 in inline style
                // (JS set them but IO never fired to reveal them)
                document.querySelectorAll('[style]').forEach(el => {
                    const s = el.style;
                    if (s.opacity === '0') s.opacity = '1';
                    const t = s.transform || '';
                    // If element is translated far off-screen, reset transform
                    if (/translateY\(\s*[5-9]\d{1,3}|translateY\(\s*1\d{3}/.test(t)) {
                        s.transform = 'none';
                    }
                });
            }
            """)
            self.log("✅ Animações forçadas ao estado final (AOS/GSAP/ScrollTrigger/WOW)")
        except Exception as e:
            self.log(f"⚠️ Aviso ao revelar animações: {e}")

    def _inline_css_imports(self, css_content, css_url):
        """Resolve @import rules inside CSS by inlining the imported content.
        This ensures @keyframes and variables defined in imported files are available offline.
        Limited to one level of depth to avoid infinite recursion.
        """
        import_pattern = re.compile(
            r'@import\s+(?:url\()?\s*["\']?([^"\'\);\s]+)["\']?\s*\)?[^;]*;',
            re.IGNORECASE
        )

        def replace_import(match):
            original  = match.group(0)
            import_url = match.group(1).strip()
            if import_url.startswith('data:'):
                return original
            abs_url = urljoin(css_url, import_url)
            imported = None
            if abs_url in self.network_resources:
                try:
                    imported = self.network_resources[abs_url]['body'].decode('utf-8', errors='ignore')
                except Exception:
                    pass
            if not imported and self.session:
                try:
                    r = self.session.get(abs_url, timeout=10, verify=False)
                    if r.status_code == 200:
                        imported = r.text
                except Exception:
                    pass
            if imported:
                return f"/* inlined: {abs_url} */\n{imported}\n"
            return original

        return import_pattern.sub(replace_import, css_content)

    def _scroll_page(self, page):
        """Scroll the page to trigger lazy loading"""
        try:
            # First, try to disable smooth scroll libraries (Lenis, Locomotive, etc.)
            page.evaluate("""
                () => {
                    // Disable Lenis smooth scroll
                    if (window.lenis) {
                        try { window.lenis.destroy(); } catch(e) {}
                    }
                    // Disable Locomotive Scroll
                    if (window.locomotiveScroll) {
                        try { window.locomotiveScroll.destroy(); } catch(e) {}
                    }
                    // Reset any scroll-behavior smooth
                    document.documentElement.style.scrollBehavior = 'auto';
                    document.body.style.scrollBehavior = 'auto';
                    
                    // Remove overflow hidden that might prevent scrolling
                    if (getComputedStyle(document.body).overflow === 'hidden') {
                        document.body.style.overflow = 'auto';
                    }
                    if (getComputedStyle(document.documentElement).overflow === 'hidden') {
                        document.documentElement.style.overflow = 'auto';
                    }
                }
            """)
            
            # Find the actual scroll container (some sites use custom containers)
            scroll_container = page.evaluate("""
                () => {
                    // Check for common scroll container patterns
                    const selectors = [
                        '[data-scroll-container]',
                        '.scroll-container',
                        '.smooth-scroll',
                        'main',
                        '#__next',
                        '#__nuxt',
                        '#app'
                    ];
                    
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.scrollHeight > window.innerHeight) {
                            return sel;
                        }
                    }
                    return null;
                }
            """)
            
            if scroll_container:
                self.log(f"🔍 Detectado container de scroll customizado: {scroll_container}")
            
            total_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
            viewport_height = page.evaluate("window.innerHeight")
            
            # Limit scroll iterations to prevent infinite loops
            max_iterations = 20
            iteration = 0
            
            current = 0
            while current < total_height and iteration < max_iterations:
                # Scroll using multiple methods for better compatibility
                page.evaluate(f"""
                    (pos) => {{
                        window.scrollTo(0, pos);
                        document.documentElement.scrollTop = pos;
                        document.body.scrollTop = pos;
                        
                        // Also try scrolling custom containers
                        const containers = document.querySelectorAll('[data-scroll-container], .scroll-container, main');
                        containers.forEach(c => {{ c.scrollTop = pos; }});
                    }}
                """, current)
                
                # 300 ms is enough for most lazy-loaders to react to the new
                # viewport position (was 600 ms — halved for speed).
                page.wait_for_timeout(300)
                current += viewport_height
                iteration += 1

                new_height = page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
                if new_height > total_height:
                    total_height = new_height

            # Scroll back to top
            page.evaluate("""
                () => {
                    window.scrollTo(0, 0);
                    document.documentElement.scrollTop = 0;
                    document.body.scrollTop = 0;
                }
            """)
            # Short settle time after returning to top (was 1 000 ms)
            page.wait_for_timeout(500)
        except Exception as e:
            self.log(f"⚠️ Erro no scroll: {e}")


def get_site_name(url):
    """Extract a clean site name from URL for the zip filename"""
    parsed = urlparse(url)
    # Get domain without www
    domain = parsed.netloc.replace('www.', '')
    # Clean special characters
    clean_name = re.sub(r'[^a-zA-Z0-9.-]', '_', domain)
    # Add path info if present (cleaned)
    if parsed.path and parsed.path != '/':
        path_part = re.sub(r'[^a-zA-Z0-9]', '_', parsed.path.strip('/'))[:30]
        clean_name = f"{clean_name}_{path_part}"
    return clean_name


def zip_directory(folder_path, output_path):
    """Create a zip file from a directory"""
    base_name = output_path.replace('.zip', '')
    shutil.make_archive(base_name, 'zip', folder_path)
    return base_name + '.zip'
