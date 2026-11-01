"""Read clients' WhatsApp messages the way a person would.

Clients write fast: no accents, typos, abbreviations and Peruvian slang ("depa", "lucas",
"300k", "dolares", "sii", "imediata"). The bot's rules and the extractor work best on
clean Spanish, so each inbound message is rewritten here once, before anything reads it.
Only spelling and shorthand change; the meaning and the numbers stay the same.
"""
import difflib
import re
import unicodedata


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFD", value or "") if unicodedata.category(c) != "Mn").casefold()


# Lima districts the bot may hear, in their canonical spelling (keys compared folded).
LIMA_DISTRICTS = [
    "Ate", "Barranco", "Breña", "Callao", "Carabayllo", "Cercado Lima", "Chorrillos", "Comas", "El Agustino",
    "Independencia", "Jesús María", "La Molina", "La Perla", "La Punta", "La Victoria", "Lince", "Los Olivos", "Lurín",
    "Magdalena", "Miraflores", "Pueblo Libre", "Puente Piedra", "Rímac", "San Borja", "San Isidro",
    "San Juan de Lurigancho", "San Juan de Miraflores", "San Luis", "San Martín de Porres", "San Miguel",
    "Santa Anita", "Surco", "Surquillo", "Villa El Salvador", "Villa María del Triunfo", "Bellavista", "Chaclacayo",
]
_FOLDED_DISTRICTS = {fold(d): d for d in LIMA_DISTRICTS}
# Nicknames and frequent mistypings that string similarity alone does not catch.
DISTRICT_SLANG = {"once": "Lince", "sjl": "San Juan de Lurigancho", "sjm": "San Juan de Miraflores", "smp": "San Martín de Porres",
                  "jm": "Jesús María", "jesus": "Jesús María", "pueblo": "Pueblo Libre", "magda": "Magdalena",
                  "magdalena del mar": "Magdalena", "santiago de surco": "Surco", "cercado": "Cercado Lima",
                  "cercado de lima": "Cercado Lima", "lima cercado": "Cercado Lima", "la vic": "La Victoria",
                  "mira": "Miraflores", "chorri": "Chorrillos", "barran": "Barranco", "villa": "Villa El Salvador"}


def is_known_district(value):
    return value in LIMA_DISTRICTS


def closest_district(value, cutoff=0.8):
    """Canonical district for a misspelled name ('miraflorez', 'jesus mria', 'chorillos'), or None."""
    key = re.sub(r"\s+", " ", fold(value)).strip()
    if key in _FOLDED_DISTRICTS:
        return _FOLDED_DISTRICTS[key]
    if key in DISTRICT_SLANG:
        return DISTRICT_SLANG[key]
    if len(key) < 4:
        return None
    # Compare only with districts of the same word count, so "la molina o" never swallows the "o".
    same_size = [d for d in _FOLDED_DISTRICTS if len(d.split()) == len(key.split())]
    found = difflib.get_close_matches(key, same_size, n=1, cutoff=cutoff)
    return _FOLDED_DISTRICTS[found[0]] if found else None


# Whole-word replacements, matched on the folded (no accents, lowercase) word.
WORD_FIXES = {
    # money
    "dolares": "dólares", "dolar": "dólares", "dolare": "dólares", "dolars": "dólares", "dollar": "dólares",
    "dollars": "dólares", "dolraes": "dólares", "dolarse": "dólares", "dolaress": "dólares", "dls": "dólares",
    "dlls": "dólares", "dolaes": "dólares", "dorales": "dólares",
    "sole": "soles", "soless": "soles", "solez": "soles", "lucas": "soles", "luca": "soles", "lukas": "soles",
    "cocos": "dólares", "coco": "dólares", "cocosos": "dólares", "verdes": "dólares", "verdecitos": "dólares",
    "millon": "millón", "millnes": "millones", "millone": "millones", "milones": "millones", "miyones": "millones",
    "miyon": "millón", "mill": "mil",
    # property
    "depa": "departamento", "depas": "departamentos", "dpto": "departamento", "dptos": "departamentos",
    "dep": "departamento", "departameto": "departamento", "departamneto": "departamento", "departamentto": "departamento",
    "deparatmento": "departamento", "apartamento": "departamento",
    "dormitrios": "dormitorios", "dormitoros": "dormitorios", 
    "dorm": "dormitorios", "dorms": "dormitorios", "domitorios": "dormitorios", "dormitorioss": "dormitorios",
    "cuartos": "dormitorios", "habitaciones": "dormitorios", "habitacion": "dormitorio",
    "abitaciones": "dormitorios", "havitaciones": "dormitorios", "habitasiones": "dormitorios", "recamaras": "dormitorios",
    "hab": "dormitorios", "banos": "baños", "bano": "baño",
    "cochera": "estacionamiento", "cocheras": "estacionamiento", "parqueo": "estacionamiento",
    "estacionamieto": "estacionamiento", "estasionamiento": "estacionamiento",
    # payment
    "kontado": "contado", "contao": "contado", "efectivo": "contado", "cash": "contado",
    "credito": "crédito", "kredito": "crédito", "creditto": "crédito", "hipoteca": "crédito hipotecario",
    # timing / delivery
    "imediata": "inmediata", "inmediato": "inmediata", "imediato": "inmediata", "inmediatamente": "inmediata",
    "inmedita": "inmediata", "inmeadiata": "inmediata", "inmedianta": "inmediata", "enseguida": "lo antes posible",
    "ahorita": "lo antes posible", "rapido": "lo antes posible", "urgente": "lo antes posible",
    "futuro": "futura", "despues": "después",
    # requests
    "fotitos": "fotos", "fotis": "fotos", "fots": "fotos", "photos": "fotos", "imajenes": "imágenes", "imagenes": "imágenes",
    "otrs": "otros", "muestrame": "muéstrame", "muestreme": "muéstreme", "mandame": "mándame",
    "enviame": "envíame", "pasame": "pásame", "asesora": "asesor", "acesor": "asesor", "asesro": "asesor",
    "llamenme": "llámenme", "llamame": "llámame", "q": "que", "xq": "porque", "pq": "porque",
    "tmb": "también", "tb": "también", "porfa": "por favor", "porfavor": "por favor", "xfa": "por favor",
    "plis": "por favor", "pls": "por favor", "nesecito": "necesito", "nececito": "necesito", "nesesito": "necesito",
    "kiero": "quiero", "qiero": "quiero", "quero": "quiero", 
}

PHRASE_FIXES = [
    (r"\bal\s*to(?:qu|k)e(?:\s+to(?:qu|k)e)*\b", "lo antes posible"),
    (r"\bya\s+mismo\b", "lo antes posible"), (r"\bcuanto\s+antes\b", "lo antes posible"),
    (r"\bpara\s+ayer\b", "lo antes posible"), (r"\bsin\s+prisa\b", "sin apuro"),
    (r"\bno\s+hay\s+prisa\b", "sin apuro"), (r"\bmas\s+adelante\b", "en los próximos años"),
]

# Key words of this conversation; a word that is close to one of them (one or two letters off) is read as it.
# Words that look alike but mean something else here ("vista"/"visita", "ascensor"/"asesor") are left out on purpose.
VOCABULARY = ["contado", "crédito", "hipotecario", "inmediata", "inmediato", "dormitorios", "dormitorio", "departamento",
              "departamentos", "estacionamiento", "estacionamientos", "dólares", "presupuesto", "financiamiento",
              "construcción", "opción", "opciones"]
_FOLDED_VOCABULARY = {fold(w): w for w in VOCABULARY}


def fuzzy_word(token):
    key = fold(token)
    if len(key) < 6 or key in _FOLDED_VOCABULARY:
        return None
    found = difflib.get_close_matches(key, list(_FOLDED_VOCABULARY), n=1, cutoff=0.82)
    return _FOLDED_VOCABULARY[found[0]] if found else None


AFFIRMATIVE = re.compile(r"^(?:s+i+|s+i+p+|si+s+|se+|se+p+|simon|sii+|ok+|oki+|okey|okay|oka|va+|vale|ya pues|"
                         r"ps\s*s+i+|pues\s*s+i+|claro|claro que si|claro q si|de una|dale+|correcto|exacto|afirmativo|asi es)"
                         r"[\s!.,]*(?:gracias|por favor|porfa)?[\s!.,]*$")
SOON = {"ahora", "ahorita", "ahora mismo", "rapido", "lo mas rapido", "de inmediato", "ya mismo", "urgente", "inmediato", "enseguida"}
NUMBER_WORDS = {"uno": "1", "un": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6"}


def _fix_money(text):
    # "1.5millones", "1,5 millones", "medio millon" and "300k" become plain amounts the parsers read.
    text = re.sub(r"(\d)(mil|millon|millón|millones|k|soles|dolares|dólares|cocos|lucas|lukas|verdes)\b", r"\1 \2", text, flags=re.I)
    text = re.sub(r"\bmedio\s+mill[oó]n\b", "500000", text, flags=re.I)

    def millions(match):
        value = float(match.group(1).replace(",", "."))
        return f"{value * 1_000_000:.0f}"
    text = re.sub(r"\b(\d+(?:[.,]\d+)?)\s*(?:mill[oó]n|millones)\b", millions, text, flags=re.I)
    text = re.sub(r"\b(\d{2,3}(?:[.,]\d)?)\s*k\b", lambda m: f"{float(m.group(1).replace(',', '.')) * 1000:.0f}", text, flags=re.I)
    return text


def normalize_client_text(text):
    """Clean spelling and shorthand of one inbound message; never changes its meaning."""
    if not text:
        return text
    original = text.strip()
    if AFFIRMATIVE.match(fold(original)):
        return "Sí"
    if fold(original).strip(" .!") in SOON:
        return "Lo antes posible"
    if fold(original).strip(" .!") in NUMBER_WORDS:
        return NUMBER_WORDS[fold(original).strip(" .!")]
    cleaned = _fix_money(original)
    for pattern, replacement in PHRASE_FIXES:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)

    def word(match):
        token = match.group(0)
        fixed = WORD_FIXES.get(fold(token)) or fuzzy_word(token)
        if fixed is None:
            return token
        return fixed.capitalize() if token[:1].isupper() and len(token) > 1 else fixed
    cleaned = re.sub(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", word, cleaned)
    cleaned = re.sub(r"(\d)\s+de\s+(soles|dólares)\b", r"\1 \2", cleaned, flags=re.I)
    # "dos dormitorios" -> "2 dormitorios"
    cleaned = re.sub(r"\b(uno|un|una|dos|tres|cuatro|cinco|seis)\s+(dormitorios?|baños?)",
                     lambda m: f"{NUMBER_WORDS[fold(m.group(1))]} {m.group(2)}", cleaned, flags=re.I)
    # A message that is just a district, misspelled ("miraflorez", "en jesus mria") gets its canonical name.
    bare = re.sub(r"^(?:en|por|de|el|la|distrito de|distrito)\s+", "", cleaned.strip(" .!¿?"), flags=re.I)
    parts = [p.strip() for p in re.split(r",|\s+y\s+|\s+o\s+|/", bare) if p.strip()]
    if parts and len(parts) <= 4 and all(len(p.split()) <= 5 for p in parts):
        strict = [closest_district(p) for p in parts]
        if any(strict):
            # In a list of districts, a badly typed name ("chrrills", "once") is read more loosely.
            loose = [d or closest_district(p, cutoff=0.6) for d, p in zip(strict, parts)]
            if all(loose):
                return " y ".join(dict.fromkeys(loose))
    cleaned = _fix_districts_in_sentence(cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _fix_districts_in_sentence(text):
    """'en surquiyo' -> 'en Surquillo': after each 'en/por/de', try the longest run of up to 4 words first."""
    tokens = re.split(r"(\s+)", text)
    words = [i for i, t in enumerate(tokens) if t.strip()]
    out = list(tokens)
    k = 0
    while k < len(words):
        if fold(tokens[words[k]]) in {"en", "por", "de"}:
            for size in range(min(4, len(words) - k - 1), 0, -1):
                span = words[k + 1:k + 1 + size]
                phrase = " ".join(tokens[i].strip(",.;!?") for i in span)
                found = closest_district(phrase)
                if found and fold(phrase) not in {"de", "la", "el"}:
                    trailing = re.search(r"[,.;!?]+$", tokens[span[-1]])
                    out[span[0]] = found + (trailing.group(0) if trailing else "")
                    for i in range(span[0] + 1, span[-1] + 1):
                        out[i] = ""
                    k += size
                    break
        k += 1
    return re.sub(r"\s{2,}", " ", "".join(out))
