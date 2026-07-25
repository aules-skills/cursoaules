#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_mbz.py — genera un backup .mbz (Moodle 2 backup format) restaurable en
Aules/Moodle, con:
  - Seccio 0 "General" (amb un forum "Anuncis")
  - Una seccio per tema, extreta del currículum (organitzacio del CONTINGUT).
    El nom de la seccio es públic. Dins de cada seccio hi ha:
      * Una etiqueta (label) OCULTA a l'alumnat (visible=0) amb les notes
        pedagogiques (competencia, criteris d'avaluacio desglossats un a un
        amb el seu text complet) — nomes el professorat la veu.
      * N Tasques (mod_assign) publiques per tema, numerades
        "Practica Tema#{n} #1" .. "Practica Tema#{n} #N". CADA Tasca avalua
        DOS criteris consecutius de la competencia principal del tema:
        #1 -> criteris 1 i 2 (p.ex. CE1.1 i CE1.2), #2 -> criteris 3 i 4
        (CE1.3 i CE1.4), #3 -> criteris 5 i 6 (CE1.5 i CE1.6), i aixi
        successivament, recorrent ciclicament si hi ha mes grups de
        practiques que parells de criteris. Com un unic grade_item de
        Moodle nomes pot pertanyer a UNA categoria, el PRIMER criteri del
        parell es la categoria "oficial" de la Tasca (grade_item tipus mod);
        el SEGON criteri es representa amb un element de qualificacio
        MANUAL (itemtype=manual) dins de la seua categoria, ja etiquetat amb
        el nom de la mateixa Tasca, perque el professorat hi introduisca la
        nota d'eixe segon criteri a ma despres de corregir el mateix treball.
    "Recursos" (H5P / paquet SCORM) NO es crea ací: Moodle exigeix pujar ja
    un paquet real en crear eixos mòduls (no hi ha "buit a l'espera de
    contingut"), aixi que es genera per separat (p.ex. amb les skills h5p o
    pasapalabra) i es puja a ma quan estiga llest.
  - Llibre de qualificacions en 3 nivells, organitzat per COMPETENCIA (no per
    tema/avaluacio):
      Curs -> Competencia especifica (pes igual entre CE)
            -> Criteri d'avaluacio (pes igual entre els criteris de la mateixa CE)

Entrada: un JSON amb l'estructura documentada a schema_example.json
(veure tambe SKILL.md). Ús:

    python3 build_mbz.py curso_data.json [output.mbz]

L'esquema XML reprodueix EXACTAMENT el que s'ha comprovat que restaura sense
errors en un Moodle 4.5.10 (Aules, GVA), AMB UNA EXCEPCIO IMPORTANT: l'esquema
de la Tasca (mod_assign) es la MILLOR RECONSTRUCCIO POSSIBLE a partir del
format de backup general de Moodle, pero NO s'ha verificat encara contra una
restauracio real (a diferencia de seccions/etiquetes/forum/gradebook, que si
ho estan). Prova-ho primer en un curs de prova abans de confiar-hi per a un
curs real amb alumnat.
"""
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import uuid
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
import zipfile
import glob
import shutil

EMPTY_SHA1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # sha1("") - fitxer "directori" de Moodle
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_recursos")


def xml_text_escape(s):
    """Escapa NOMÉS els caracters que XML exigeix en contingut de text
    (&, <, >). Aixo es CRÍTIC: si un camp de text conté HTML (<p>,
    <strong>...) i no s'escapa, Moodle el rebutja en restaurar (queden
    com a subelements XML en compte de com a text pla)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_data(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_ASSET_CACHE = {}

# Alguns paquets SCORM son literalment un .zip, i alguns instal·ladors de
# skills reboten el paquet .skill sencer si detecten un .zip niat a dins
# (missatge "Zip cannot contain nested zip files"). Per evitar-ho, el fitxer
# es guarda al disc amb una extensio diferent (vore ASSET_DISK_RENAME) pero
# es registra a Moodle amb el seu nom ORIGINAL .zip (imprescindible perque
# mod_scorm el reconega com a paquet SCORM valid en restaurar).
ASSET_DISK_RENAME = {
    "Pasapalabra_SCORM.zip": "Pasapalabra_SCORM.scormpkg",
    "Ahorcado_etiquetas_HTML.zip": "Ahorcado_etiquetas_HTML.scormpkg",
    "BinaryGame_SCORM.zip": "BinaryGame_SCORM.scormpkg",
}


def load_asset(filename):
    """Llig (amb cache) un fitxer de assets_recursos/ i torna els seus bytes,
    grandaria i sha1. `filename` es sempre el nom LOGIC/de Moodle (p.ex.
    acabat en .zip); si eixe nom esta a ASSET_DISK_RENAME, es llig del disc
    amb el nom renombrat mentre que `info["filename"]` manté el nom logic
    original per a registrar-lo a Moodle."""
    if filename in _ASSET_CACHE:
        return _ASSET_CACHE[filename]
    disk_name = ASSET_DISK_RENAME.get(filename, filename)
    path = os.path.join(ASSETS_DIR, disk_name)
    with open(path, "rb") as f:
        data = f.read()
    info = {"filename": filename, "bytes": data, "size": len(data),
            "sha1": hashlib.sha1(data).hexdigest()}
    _ASSET_CACHE[filename] = info
    return info


_SCORM_INNER_CACHE = {}


def scorm_inner_info(filename):
    """Extrau d'un .zip SCORM (index.html + imsmanifest.xml) tot el que fa
    falta per a reconstruir el <scorm> real de Moodle: identificador del
    manifest, titols d'organitzacio/item, masteryscore, i el contingut+hash
    dels dos fitxers interns (que Moodle tambe emmagatzema per separat, com
    a filearea=content, a mes del .zip original com a filearea=package)."""
    if filename in _SCORM_INNER_CACHE:
        return _SCORM_INNER_CACHE[filename]
    asset = load_asset(filename)
    z = zipfile.ZipFile(io.BytesIO(asset["bytes"]))
    index_bytes = z.read("index.html")
    manifest_bytes = z.read("imsmanifest.xml")
    manifest_text = manifest_bytes.decode("utf-8", errors="replace")

    def find(pattern, default=""):
        m = re.search(pattern, manifest_text, re.S)
        return m.group(1).strip() if m else default

    info = {
        "manifest_id": find(r'<manifest\s+identifier="([^"]+)"', "SCORM"),
        "org_title": find(r"<organization[^>]*>\s*<title>([^<]*)</title>"),
        "item_title": find(r"<item[^>]*>\s*<title>([^<]*)</title>"),
        "mastery": find(r"<adlcp:masteryscore>([^<]*)</adlcp:masteryscore>", "5"),
        "index_bytes": index_bytes,
        "index_size": len(index_bytes),
        "index_sha1": hashlib.sha1(index_bytes).hexdigest(),
        "manifest_bytes": manifest_bytes,
        "manifest_size": len(manifest_bytes),
        "manifest_sha1": hashlib.sha1(manifest_bytes).hexdigest(),
    }
    _SCORM_INNER_CACHE[filename] = info
    return info


def build(data, out_path):
    NOW = int(time.time())
    tmp_dir = f"_mbz_build_{NOW}_{os.getpid()}"
    os.makedirs(tmp_dir, exist_ok=True)

    def write(relpath, content):
        full = os.path.join(tmp_dir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def write_binary(relpath, data_bytes):
        full = os.path.join(tmp_dir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data_bytes)

    XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'

    course_shortname = data["course_shortname"]
    course_fullname = data["course_fullname"]
    course_summary = data.get("course_summary", "")
    wwwroot = data.get("wwwroot", "https://aules.edu.gva.es/docent")
    moodle_version = data.get("moodle_version", "2024100710.08")
    moodle_release = data.get("moodle_release", "4.5.10+ (Build: 20260403)")
    site_hash = data.get("site_identifier_hash", "276defd03abf8ae32f54c6bf719ce3f6")
    num_evaluacions = data.get("num_evaluacions", 3)
    eval_names = data.get(
        "eval_names",
        {str(i): f"{i}a Avaluacio" for i in range(1, num_evaluacions + 1)},
    )
    temas = data["temas"]
    competencias = data["competencias"]
    general_intro_html = data.get(
        "general_intro_html",
        f"<p>Benvinguts/des a {course_fullname}.</p>",
    )
    filename = data.get("filename", f"{course_shortname}.mbz")

    # ---- Mode nou: "evaluacio_weights" (dict amb activitats/recursos/examen
    # en % 0-100) activa l'estructura anidada Curs -> Avaluacio ->
    # {Activitats(CE->Criteri) / Recursos / Examen}, amb una practica per
    # CADA criteri de cada tema (no per parelles) i una seccio "Examen" nova
    # al final de cada avaluacio que qualifica totes les CE d'eixa avaluacio.
    # Si no s'indica, es manté el mode pla Curs -> CE -> Criteri (compatible
    # amb tots els cursos generats fins ara). ----
    EVAL_WEIGHTS = data.get("evaluacio_weights")
    NEW_MODE = bool(EVAL_WEIGHTS)
    if NEW_MODE:
        w_act = float(EVAL_WEIGHTS.get("activitats", 50))
        w_rec = float(EVAL_WEIGHTS.get("recursos", 30))
        w_exa = float(EVAL_WEIGHTS.get("examen", 20))
        _tw = w_act + w_rec + w_exa
        if _tw <= 0:
            w_act, w_rec, w_exa = 50.0, 30.0, 20.0
        elif abs(_tw - 100.0) > 0.01:
            w_act, w_rec, w_exa = (w_act / _tw * 100.0, w_rec / _tw * 100.0, w_exa / _tw * 100.0)
        CRITERIS_PER_PRACTICA = 1
        NUM_PRACTIQUES = int(data.get("num_practiques_per_tema", 4))
    else:
        NUM_PRACTIQUES = data.get("num_practiques_per_tema", 3)
        CRITERIS_PER_PRACTICA = data.get("criteris_per_practica", 1)

    # ---- ids: comptador seqüencial unic, per evitar qualsevol col·lisio
    # independentment de quants temes/competencies/criteris hi haja ----
    _counter = [900000]

    def next_id():
        _counter[0] += 1
        return _counter[0]

    # ---- registre global de fitxers (files.xml) i dels seus blobs binaris
    # (per als Recursos H5P/SCORM reals). Cada component de Moodle que puja
    # un fitxer (mod_h5pactivity, mod_scorm...) necessita un registre "." de
    # directori (filesize 0, sha1 buit) MES UN registre pel fitxer real, amb
    # el mateix esquema de camps que un backup real d'Aules. Els blobs es
    # deduplicen per sha1 (mateix contingut = mateix fitxer al tar, encara
    # que l'usen moltes activitats), igual que fa Moodle de veritat.
    FILES_REGISTRY = []
    BLOBS_TO_WRITE = {}

    def add_file_record(contextid, component, filearea, filename, filepath="/",
                         itemid=0, content_bytes=None, mimetype="$@NULL@$",
                         author="$@NULL@$", license_="$@NULL@$", source=None):
        fid = next_id()
        if content_bytes is None or len(content_bytes) == 0:
            sha1 = EMPTY_SHA1
            size = 0
        else:
            sha1 = hashlib.sha1(content_bytes).hexdigest()
            size = len(content_bytes)
            BLOBS_TO_WRITE[sha1] = content_bytes
        if source is None:
            source = "$@NULL@$" if filename == "." else filename
        FILES_REGISTRY.append({
            "id": fid, "contenthash": sha1, "contextid": contextid,
            "component": component, "filearea": filearea, "itemid": itemid,
            "filepath": filepath, "filename": filename, "filesize": size,
            "mimetype": mimetype, "source": source,
            "author": author, "license": license_,
        })
        return fid

    COURSE_ID = next_id()
    COURSE_CTX = next_id()
    SEC_GENERAL = next_id()
    SEC_TEMA = {t["n"]: next_id() for t in temas}
    FORUM_MODULEID = next_id()
    FORUM_INSTANCEID = next_id()
    FORUM_CTX = next_id()

    # CAT_CRIT_ORDER (codi_ce -> llista ordenada de codis de criteri) fa
    # falta ja des d'ara per a calcular quantes practiques du cada tema en
    # el mode nou.
    CAT_CRIT_ORDER = {}
    for comp in competencias:
        CAT_CRIT_ORDER[comp["codigo"]] = [c["codigo"] for c in comp.get("criterios", [])]

    # CRIT_TIPO: (codi_ce, crit_codi) -> "concepto" | "accion", tal com ve
    # marcat a cada criteri del JSON (camp "tipo"). Servix per decidir si un
    # criteri l'avalua un Recurs (H5P/SCORM, si es "concepto": identificar,
    # descriure...) o una Practica rubricada (si es "accion": procediment
    # fisic/demostrable: muntar, connectar, instal·lar, comprovar...). Si un
    # criteri no porta "tipo" (currículums antics sense classificar), es
    # tracta per defecte com "accion" (practica), que es el comportament que
    # ja tenia la skill abans d'esta distincio.
    CRIT_TIPO = {}
    for comp in competencias:
        codi = comp["codigo"]
        for c in comp.get("criterios", []):
            CRIT_TIPO[(codi, c["codigo"])] = c.get("tipo", "accion")

    # TEMA_INDEX_PER_CE (n tema -> posicio 0-indexada entre els temes que
    # comparteixen la mateixa CE com a principal, en ordre de n). Es calcula
    # ja des d'ara (abans nomes es calculava mes avant, junt amb els
    # Recursos) perque tambe el necessita practica_items() com a fallback
    # per a temes sense criterios_tema.
    TEMA_INDEX_PER_CE = {}
    _count_per_ce_tmp = {}
    for _t in temas:
        _codi = (_t.get("competencia") or {}).get("codi", "")
        TEMA_INDEX_PER_CE[_t["n"]] = _count_per_ce_tmp.get(_codi, 0)
        _count_per_ce_tmp[_codi] = _count_per_ce_tmp.get(_codi, 0) + 1

    def tema_criterios(t):
        """Llista de (codi_ce, crit_codi) que treballa aquest tema. Si el
        JSON indica \`criterios_tema\` (extracció exacta de quins criteris
        assigna el currículum a esta unitat concreta, siga la seua CE
        principal o una CE secundaria), s'usa tal qual. Si no (mode antic /
        currículums sense esta extraccio), es reconstrueix a partir de TOTS
        els criteris de la CE principal del tema."""
        ct = t.get("criterios_tema")
        if ct:
            return [(item["ce"], item["crit"]) for item in ct]
        comp = t.get("competencia") or {}
        codi = comp.get("codi", "")
        return [(codi, c) for c in CAT_CRIT_ORDER.get(codi, [])]

    def practica_items(t):
        """Llista (codi_ce, crit_codi), UNA entrada per Practica, en l'ordre
        en que es generaran. Amb criterios_tema: SEMPRE NUM_PRACTIQUES
        entrades (per defecte 4, fix per a tots els temes), ciclant pels
        criteris d'eixe tema classificats com \"accion\" (repetint-los si
        n'hi ha menys de NUM_PRACTIQUES). Si el tema no te CAP criteri
        \"accion\" propi, cicla en el seu lloc per TOTS els criteris del
        tema (siga quin siga el seu tipus), per a que cap tema es quede
        sense les seues 4 Practiques. Sense criterios_tema (fallback, mode
        antic): es manté el comportament previ — NUM_PRACTIQUES entrades
        fixes, rotant pels criteris de la CE principal segons la posicio
        del tema entre els que la comparteixen (TEMA_INDEX_PER_CE)."""
        ct = t.get("criterios_tema")
        if ct:
            n_prac = NUM_PRACTIQUES if NUM_PRACTIQUES else 4
            accio = [(ce, crit) for (ce, crit) in tema_criterios(t)
                     if CRIT_TIPO.get((ce, crit), "accion") != "concepto"]
            pool = accio if accio else tema_criterios(t)
            if not pool:
                return []
            return [pool[i % len(pool)] for i in range(n_prac)]
        comp = t.get("competencia") or {}
        codi = comp.get("codi", "")
        crits = CAT_CRIT_ORDER.get(codi, [])
        if not crits:
            return []
        offset = TEMA_INDEX_PER_CE.get(t["n"], 0)
        n_prac = NUM_PRACTIQUES if NUM_PRACTIQUES else 4
        return [(codi, crits[(k - 1 + offset) % len(crits)]) for k in range(1, n_prac + 1)]

    def practica_ce_crit(t, k):
        """(codi_ce, crit_codi) que avalua la Practica numero k (1-indexada)
        del tema t, o (None, None) si k esta fora de rang."""
        items = practica_items(t)
        if 1 <= k <= len(items):
            return items[k - 1]
        return (None, None)

    def num_practiques_per_tema(t):
        """Nombre de practiques del tema: amb criterios_tema, exactament els
        seus criteris \"accion\" (pot variar d'un tema a un altre, ja que
        cada unitat del currículum en treballa un nombre distint). Sense
        criterios_tema (fallback), es manté el nombre FIX configurable via
        num_practiques_per_tema al JSON (per defecte 4 en mode nou, 3 en
        mode classic)."""
        if t.get("criterios_tema"):
            return len(practica_items(t))
        return NUM_PRACTIQUES

    # una etiqueta (label) OCULTA a l'alumnat per tema, amb les notes
    # pedagogiques (competencia/criteris) nomes visibles per al professorat
    LABEL_MODULEID = {t["n"]: next_id() for t in temas}
    LABEL_INSTANCEID = {t["n"]: next_id() for t in temas}
    LABEL_CTX = {t["n"]: next_id() for t in temas}
    # tantes Tasques (mod_assign) publiques "Practica Tema#n #k" per tema com
    # torne num_practiques_per_tema(t)
    ASSIGN_MODULEID = {t["n"]: {k: next_id() for k in range(1, num_practiques_per_tema(t) + 1)} for t in temas}
    ASSIGN_INSTANCEID = {t["n"]: {k: next_id() for k in range(1, num_practiques_per_tema(t) + 1)} for t in temas}
    ASSIGN_CTX = {t["n"]: {k: next_id() for k in range(1, num_practiques_per_tema(t) + 1)} for t in temas}

    # ---- Estructura de curs (seccions): en el mode nou, s'intercala una
    # seccio "Examen" nova al final dels temes de cada avaluacio. En el mode
    # classic, nomes hi ha seccions de tema, en l'ordre del JSON. ----
    temas_by_eval = {}
    for t in temas:
        temas_by_eval.setdefault(t["eval"], []).append(t)

    course_items = []  # llista de ("tema", t) | ("examen", ev)
    if NEW_MODE:
        for ev in range(1, num_evaluacions + 1):
            for t in temas_by_eval.get(ev, []):
                course_items.append(("tema", t))
            if temas_by_eval.get(ev):
                course_items.append(("examen", ev))
    else:
        course_items = [("tema", t) for t in temas]

    POSITION = {}  # ("tema", n) | ("examen", ev) -> posicio seqüencial (1, 2, 3...)
    _pos = 1
    for kind, obj in course_items:
        if kind == "tema":
            POSITION[("tema", obj["n"])] = _pos
        else:
            POSITION[("examen", obj)] = _pos
        _pos += 1

    EVALS_AMB_EXAMEN = [ev for ev in range(1, num_evaluacions + 1) if temas_by_eval.get(ev)] if NEW_MODE else []
    SEC_EXAMEN = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_LABEL_MODULEID = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_LABEL_INSTANCEID = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_LABEL_CTX = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_ASSIGN_MODULEID = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_ASSIGN_INSTANCEID = {ev: next_id() for ev in EVALS_AMB_EXAMEN}
    EXAM_ASSIGN_CTX = {ev: next_id() for ev in EVALS_AMB_EXAMEN}

    # CEs (principals + transversals) que qualifica l'examen de cada
    # avaluacio: totes les que apareixen com a competencia principal o
    # secundaria/transversal en algun tema d'eixa avaluacio (deduplicades,
    # en ordre d'aparicio). L'examen es l'unica activitat que arriba a
    # qualificar tambe les competencies transversals (les practiques nomes
    # qualifiquen la competencia principal del seu tema).
    CES_EXAMEN_PER_EVAL = {}
    for ev in EVALS_AMB_EXAMEN:
        codis = []
        for t in temas_by_eval.get(ev, []):
            comp = t.get("competencia") or {}
            codi = comp.get("codi")
            if codi and codi not in codis:
                codis.append(codi)
            for extra in t.get("competencies_extra", []):
                codi_extra = extra.get("codi")
                if codi_extra and codi_extra not in codis:
                    codis.append(codi_extra)
        CES_EXAMEN_PER_EVAL[ev] = codis

    # Llibre de qualificacions:
    #  - Mode classic: Curs -> CE -> Criteri (pes igual entre CE i entre
    #    criteris d'una mateixa CE), igual que sempre.
    #  - Mode nou (CE-anidat): Curs -> Avaluacio (pes igual entre avaluacions)
    #      -> CE (nomes les principals d'eixa avaluacio, pes igual entre
    #         elles)
    #          -> Activitats (w_act%) -> Criteri (pes igual)
    #          -> Recursos (w_rec%) -> 6 jocs H5P/SCORM per tema d'eixa CE
    #          -> Examen (w_exa%): nota d'eixa CE dins l'examen de l'avaluacio
    #      -> a mes, si hi ha alguna CE transversal (competencies_extra) que
    #         l'examen tambe avalua pero cap tema la te com a principal, es
    #         crea una CE "nomes examen" (sense Activitats ni Recursos, ja
    #         que les practiques i els recursos mai la toquen).
    CAT_ROOT = next_id()
    CAT_CE = {}
    CAT_CRIT = {}
    CAT_AVAL = {}
    CAT_ACTIVITATS = {}
    CAT_RECURSOS = {}
    CAT_CRIT_RECURSOS = {}  # (ev, codi_ce, crit_codi) -> id: subcategoria de
    # Recursos per a cada criteri, mirror exacte de CAT_CRIT davall
    # Activitats. Els Recursos avaluen aixi el mateix criteri concret que
    # avaluen les Practiques, en lloc de qualificar nomes la CE sencera.
    CAT_EXAMEN = {}
    # ev -> llista de codis de CE que tenen ALGUN criteri actiu (Practica o
    # Recurs) en eixa avaluacio -- ja siga com a CE principal d'algun tema
    # (mode antic) o com a qualsevol CE referenciada per criterios_tema
    # d'algun tema (mode nou amb extraccio exacta, on una CE secundaria com
    # RA2 a UD3/UD4 tambe pot tindre les seues propies Practiques/Recursos,
    # no nomes apareixer a l'Examen). El nom es manté (CE_PRINCIPALS_PER_EVAL)
    # per no tocar la resta de referencies, encara que ara inclou mes CEs
    # que nomes les "principals" en sentit estricte.
    CE_PRINCIPALS_PER_EVAL = {}

    if NEW_MODE:
        for ev in range(1, num_evaluacions + 1):
            if not temas_by_eval.get(ev):
                continue
            CAT_AVAL[ev] = next_id()
            ce_principals = []
            for t in temas_by_eval[ev]:
                for (codi, _crit) in tema_criterios(t):
                    if codi and codi not in ce_principals:
                        ce_principals.append(codi)
            CE_PRINCIPALS_PER_EVAL[ev] = ce_principals
            for codi in ce_principals:
                CAT_CE[(ev, codi)] = next_id()
                CAT_ACTIVITATS[(ev, codi)] = next_id()
                CAT_RECURSOS[(ev, codi)] = next_id()
                CAT_EXAMEN[(ev, codi)] = next_id()
                for crit_codi in CAT_CRIT_ORDER.get(codi, []):
                    CAT_CRIT[(ev, codi, crit_codi)] = next_id()
                    CAT_CRIT_RECURSOS[(ev, codi, crit_codi)] = next_id()
            # CE transversals que l'examen avalua pero que cap tema d'esta
            # avaluacio te com a principal (p.ex. una competencia transversal
            # com CE5): reben nomes una branca CE -> Examen (100%).
            for codi in CES_EXAMEN_PER_EVAL.get(ev, []):
                if codi in ce_principals:
                    continue
                if (ev, codi) in CAT_CE:
                    continue
                CAT_CE[(ev, codi)] = next_id()
                CAT_EXAMEN[(ev, codi)] = next_id()
    else:
        for comp in competencias:
            CAT_CE[comp["codigo"]] = next_id()
        for comp in competencias:
            for crit in comp.get("criterios", []):
                CAT_CRIT[(comp["codigo"], crit["codigo"])] = next_id()

    # 6 Recursos (H5P/SCORM reals) per tema, nomes en mode nou. Cada un
    # avalua la competencia PRINCIPAL del tema (mai la transversal), igual
    # que les Practiques.
    RECURSOS_GAMES = [
        ("pasapalabra", "scorm", "Pasapalabra", "Pasapalabra_SCORM.zip"),
        ("ahorcado", "scorm", "Ahorcat", "Ahorcado_etiquetas_HTML.zip"),
        ("crossword", "h5p", "Crucigrama", "Crossword_sistemas_numeracion_4ESO.h5p"),
        ("wordsearch", "h5p", "Sopa de lletres", "Sopa_lletres_PLACEHOLDER.h5p"),
        ("blanks", "h5p", "Omplir buits", "Netiqueta_huecos_4ESO.h5p"),
        ("dragtext", "h5p", "Arrossegar paraules", "Arrastra_etiquetas_HTML_20.h5p"),
    ]
    def num_recursos_per_tema(codi_ce):
        """Nombre de Recursos que ha de generar un tema associat a `codi_ce`.
        Per defecte 6 (un per cada tipus de joc de RECURSOS_GAMES), pero si
        la CE te MES de 6 criteris es generen tants Recursos com criteris
        (ciclant els 6 tipus de joc, repetint-los si cal) — aixi un sol tema
        garanteix cobrir TOTS els criteris de la seua CE amb Recursos, encara
        que siga l'unic tema d'eixa CE en la seua avaluacio."""
        crits = CAT_CRIT_ORDER.get(codi_ce, [])
        return max(6, len(crits)) if crits else 6

    def recurso_game_info(recurso_index):
        """Torna (slot, tipo, nom, fitxer) per al Recurs `recurso_index`
        (0-indexat) d'un tema. Cicla els 6 tipus de joc fixos; si
        recurso_index >= 6 (la CE te mes de 6 criteris) repeteix el tipus de
        joc pero afig un sufix "(2)", "(3)"... al nom i a la clau interna
        per distingir-los com a activitats i elements de calificacio
        separats."""
        slot, tipo, nom_base, fitxer = RECURSOS_GAMES[recurso_index % 6]
        ronda = recurso_index // 6
        if ronda == 0:
            return slot, tipo, nom_base, fitxer
        return f"{slot}_{ronda + 1}", tipo, f"{nom_base} ({ronda + 1})", fitxer

    # Jocs EXTRA: no formen part del cicle fix de 6 (RECURSOS_GAMES), sino
    # que s'afigen nomes a temes concrets que ho demanen explicitament al
    # JSON (camp `recursos_extra`, llista de claus d'aquest diccionari).
    # Pensat per a habilitats molt especifiques d'un sol tema — p.ex. el
    # Binary Game (practicar conversio binari-decimal) nomes te sentit al
    # tema de sistemes numerics, no en tots els temes de la seua CE.
    EXTRA_GAMES = {
        "binarygame": ("binarygame", "scorm", "Binary Game", "BinaryGame_SCORM.zip"),
    }

    def extra_recursos_for_tema(t):
        """Llista de claus de EXTRA_GAMES demanades pel tema `t` via el
        camp `recursos_extra` del JSON (p.ex. ["binarygame"]). Buida si no
        en te cap."""
        return [k for k in t.get("recursos_extra", []) if k in EXTRA_GAMES]

    def games_for_tema(codi_ce, t):
        """Torna la llista completa (slot, tipo, nom, fitxer) dels Recursos
        d'un tema: primer els normals (num_recursos_per_tema(codi_ce),
        ciclant RECURSOS_GAMES), i despres els extra propis d'aquest tema
        (vore extra_recursos_for_tema) — aquests ultims NO couten en el
        cicle dels 6 jocs normals ni es repeteixen a altres temes."""
        games = [recurso_game_info(i) for i in range(num_recursos_per_tema(codi_ce))]
        for key in extra_recursos_for_tema(t):
            games.append(EXTRA_GAMES[key])
        return games

    def num_recursos_total_per_tema(codi_ce, t):
        """num_recursos_per_tema(codi_ce) mes els extra d'aquest tema en
        concret (vore games_for_tema)."""
        return num_recursos_per_tema(codi_ce) + len(extra_recursos_for_tema(t))

    # RECURSO_MODULEID/INSTANCEID/CTX i TEMA_INDEX_PER_CE es calculen mes
    # avant (vore mes avall, despres de criteri_per_recurs), perque el nou
    # mecanisme content-driven (recurso_items_full) necessita eixa funcio ja
    # definida. TEMA_INDEX_PER_CE ja es va calcular abans (a l'inici de
    # build(), junt amb practica_items) i es reutilitza ací.
    RECURSO_MODULEID = {}
    RECURSO_INSTANCEID = {}
    RECURSO_CTX = {}

    def criteris_per_practica_k(codi_ce, k, tema_offset=0):
        """Torna la llista de codis de criteri que ha d'avaluar la practica
        numero `k` (1-indexat) d'un tema associat a la competencia `codi_ce`.

        En mode nou (NEW_MODE), cada practica avalua UN sol criteri, triat
        per index rotatori `(k - 1 + tema_offset) % len(crits)`: `tema_offset`
        es la posicio del tema entre els que comparteixen eixa CE com a
        principal (TEMA_INDEX_PER_CE). Com que ara el nombre de practiques
        per tema es FIX (per defecte 4) i pot no coincidir amb el nombre de
        criteris de la CE, aquesta rotacio es la que garanteix que, quan
        diversos temes comparteixen la mateixa CE, entre tots acaben cobrint
        tots els seus criteris en lloc de repetir sempre els primers —
        exactament el mateix mecanisme que ja fan servir els Recursos (vore
        criteri_per_recurs).

        En mode classic, es manté el comportament original: divideix la
        llista de criteris en grups consecutius de mida CRITERIS_PER_PRACTICA
        i els recorre ciclicament (tema_offset s'ignora).

        Torna [] si la competencia no te cap criteri definit."""
        crits = CAT_CRIT_ORDER.get(codi_ce, [])
        if not crits:
            return []
        if NEW_MODE:
            idx = (k - 1 + tema_offset) % len(crits)
            return [crits[idx]]
        n_grups = max(1, -(-len(crits) // CRITERIS_PER_PRACTICA))  # ceil
        idx = (k - 1) % n_grups
        start = idx * CRITERIS_PER_PRACTICA
        return crits[start:start + CRITERIS_PER_PRACTICA]

    def categories_per_practica(codi_ce, k, ev=None, tema_offset=0):
        """Torna la llista d'ids de categoria (gradebook) corresponents als
        criteris de criteris_per_practica_k(). En el mode nou cada criteri
        viu davall la seua avaluacio (ev), aixi que cal indicar-la."""
        crits = criteris_per_practica_k(codi_ce, k, tema_offset)
        if NEW_MODE:
            return [CAT_CRIT.get((ev, codi_ce, c)) for c in crits]
        return [CAT_CRIT[(codi_ce, c)] for c in crits]

    def criteri_per_recurs(codi_ce, slot_index, tema_offset=0):
        """Torna el codi de criteri que ha d'avaluar el Recurs `slot_index`
        (0-indexat, seguint l'ordre fixe de RECURSOS_GAMES) d'una competencia
        `codi_ce`. Cada Recurs avalua UN criteri concret, igual que cada
        Practica. `tema_offset` es la posicio del tema entre els que
        comparteixen eixa CE com a principal (vore TEMA_INDEX_PER_CE):
        s'afig a l'index abans de fer modul, per a que — quan la CE te mes
        de 6 criteris i diversos temes la comparteixen — cada tema "rote" 6
        criteris diferents en lloc de repetir sempre els 6 primers, i entre
        tots els temes s'acabe cobrint tota la llista (si n'hi ha prou
        temes). Si nomes hi ha 1 tema i mes de 6 criteris, alguns criteris
        es queden sense recurs — limitacio estructural inevitable amb
        nomes 6 jocs fixos, no un error. Torna None si la CE no te cap
        criteri."""
        crits = CAT_CRIT_ORDER.get(codi_ce, [])
        if not crits:
            return None
        return crits[(slot_index + tema_offset) % len(crits)]

    def recurso_items(t):
        """Llista (codi_ce, crit_codi), UNA entrada per Recurs \"normal\"
        (sense comptar els extra), en l'ordre en que es generaran. Amb
        criterios_tema: SEMPRE len(RECURSOS_GAMES) entrades (un joc de
        CADA tipus, per a tots els temes per igual), ciclant pels criteris
        d'eixe tema classificats com \"concepto\" (repetint-los si n'hi ha
        menys jocs que criteris... o menys criteris que jocs). Si el tema
        no te CAP criteri \"concepto\" propi, cicla en el seu lloc per TOTS
        els criteris del tema. Sense criterios_tema (fallback, mode antic):
        es manté el comportament previ -- num_recursos_per_tema(ce) entrades
        (minim 6), rotant pels criteris de la CE principal segons la posicio
        del tema entre els que la comparteixen."""
        ct = t.get("criterios_tema")
        if ct:
            n_rec = len(RECURSOS_GAMES)
            concepte = [(ce, crit) for (ce, crit) in tema_criterios(t)
                        if CRIT_TIPO.get((ce, crit), "accion") == "concepto"]
            pool = concepte if concepte else tema_criterios(t)
            if not pool:
                return []
            return [pool[i % len(pool)] for i in range(n_rec)]
        comp = t.get("competencia") or {}
        codi = comp.get("codi", "")
        crits = CAT_CRIT_ORDER.get(codi, [])
        if not crits:
            return []
        offset = TEMA_INDEX_PER_CE.get(t["n"], 0)
        out = []
        for i in range(num_recursos_per_tema(codi)):
            crit = criteri_per_recurs(codi, i, offset)
            if crit:
                out.append((codi, crit))
        return out

    def recurso_items_full(t):
        """Llista completa (codi_ce, crit_codi, slot, tipo, nom, fitxer) dels
        Recursos d'un tema: primer un Recurs per cada element de
        recurso_items(t) (ciclant els 6 jocs fixos de RECURSOS_GAMES segons
        la seua posicio), i despres els Recursos EXTRA propis del tema
        (recursos_extra) -- que reusen el criteri del primer Recurs normal
        (o el primer criteri del tema en general si no en te cap de
        \"concepto\"), ja que un joc extra no te un criteri \"concepto\" propi
        pero ha de qualificar-ne algun."""
        base = recurso_items(t)
        out = []
        for i, (ce, crit) in enumerate(base):
            slot, tipo, nom, fitxer = recurso_game_info(i)
            out.append((ce, crit, slot, tipo, nom, fitxer))
        extra_keys = extra_recursos_for_tema(t)
        if extra_keys:
            if base:
                fallback_ce, fallback_crit = base[0]
            else:
                tc = tema_criterios(t)
                fallback_ce, fallback_crit = tc[0] if tc else (None, None)
            for key in extra_keys:
                slot, tipo, nom, fitxer = EXTRA_GAMES[key]
                out.append((fallback_ce, fallback_crit, slot, tipo, nom, fitxer))
        return out

    if NEW_MODE:
        for t in temas:
            n = t["n"]
            for ridx in range(len(recurso_items_full(t))):
                RECURSO_MODULEID[(n, ridx)] = next_id()
                RECURSO_INSTANCEID[(n, ridx)] = next_id()
                RECURSO_CTX[(n, ridx)] = next_id()

    # ============================================================
    # moodle_backup.xml
    # ============================================================
    activities_block = f"""      <activities>
        <activity>
          <moduleid>{FORUM_MODULEID}</moduleid>
          <sectionid>{SEC_GENERAL}</sectionid>
          <modulename>forum</modulename>
          <title>Anuncis</title>
          <directory>activities/forum_{FORUM_MODULEID}</directory>
          <insubsection></insubsection>
        </activity>
"""
    for kind, obj in course_items:
        if kind == "tema":
            t = obj
            n = t["n"]
            activities_block += f"""        <activity>
          <moduleid>{LABEL_MODULEID[n]}</moduleid>
          <sectionid>{SEC_TEMA[n]}</sectionid>
          <modulename>label</modulename>
          <title>Notes del professorat — Tema {n}</title>
          <directory>activities/label_{LABEL_MODULEID[n]}</directory>
          <insubsection></insubsection>
        </activity>
"""
            for k in range(1, num_practiques_per_tema(t) + 1):
                activities_block += f"""        <activity>
          <moduleid>{ASSIGN_MODULEID[n][k]}</moduleid>
          <sectionid>{SEC_TEMA[n]}</sectionid>
          <modulename>assign</modulename>
          <title>Practica Tema#{n} #{k}</title>
          <directory>activities/assign_{ASSIGN_MODULEID[n][k]}</directory>
          <insubsection></insubsection>
        </activity>
"""
            if NEW_MODE:
                for ridx, (_ce, _crit, _slot, tipo, nom, _fitxer) in enumerate(recurso_items_full(t)):
                    modname = "h5pactivity" if tipo == "h5p" else "scorm"
                    rmid = RECURSO_MODULEID[(n, ridx)]
                    activities_block += f"""        <activity>
          <moduleid>{rmid}</moduleid>
          <sectionid>{SEC_TEMA[n]}</sectionid>
          <modulename>{modname}</modulename>
          <title>Recurs Tema#{n} — {nom}</title>
          <directory>activities/{modname}_{rmid}</directory>
          <insubsection></insubsection>
        </activity>
"""
        else:
            ev = obj
            ev_name = eval_names.get(str(ev), f"{ev}a Avaluacio")
            activities_block += f"""        <activity>
          <moduleid>{EXAM_LABEL_MODULEID[ev]}</moduleid>
          <sectionid>{SEC_EXAMEN[ev]}</sectionid>
          <modulename>label</modulename>
          <title>Notes del professorat — Examen {ev_name}</title>
          <directory>activities/label_{EXAM_LABEL_MODULEID[ev]}</directory>
          <insubsection></insubsection>
        </activity>
        <activity>
          <moduleid>{EXAM_ASSIGN_MODULEID[ev]}</moduleid>
          <sectionid>{SEC_EXAMEN[ev]}</sectionid>
          <modulename>assign</modulename>
          <title>Examen {ev_name}</title>
          <directory>activities/assign_{EXAM_ASSIGN_MODULEID[ev]}</directory>
          <insubsection></insubsection>
        </activity>
"""
    activities_block += "      </activities>\n"

    sections_list = [(SEC_GENERAL, "0")]
    for kind, obj in course_items:
        if kind == "tema":
            n = obj["n"]
            sections_list.append((SEC_TEMA[n], str(POSITION[("tema", n)])))
        else:
            ev = obj
            sections_list.append((SEC_EXAMEN[ev], str(POSITION[("examen", ev)])))

    sections_block = "      <sections>\n"
    for sid, title in sections_list:
        sections_block += f"""        <section>
          <sectionid>{sid}</sectionid>
          <title>{title}</title>
          <directory>sections/section_{sid}</directory>
          <parentcmid></parentcmid>
          <modname></modname>
        </section>
"""
    sections_block += "      </sections>\n"

    course_block = f"""      <course>
        <courseid>{COURSE_ID}</courseid>
        <title>{course_shortname}</title>
        <directory>course</directory>
      </course>
"""

    root_settings = ["filename", "imscc11", "users", "anonymize", "role_assignments",
                      "activities", "blocks", "files", "filters", "comments", "badges",
                      "calendarevents", "userscompletion", "logs", "grade_histories",
                      "questionbank", "groups", "competencies", "customfield",
                      "contentbankcontent", "xapistate", "legacyfiles"]
    root_values = {
        "filename": filename, "imscc11": "0", "users": "0", "anonymize": "0",
        "role_assignments": "0", "activities": "1", "blocks": "1", "files": "1",
        "filters": "1", "comments": "0", "badges": "1", "calendarevents": "1",
        "userscompletion": "0", "logs": "0", "grade_histories": "0",
        "questionbank": "1", "groups": "1", "competencies": "0", "customfield": "1",
        "contentbankcontent": "1", "xapistate": "0", "legacyfiles": "1",
    }
    settings_block = "    <settings>\n"
    for name in root_settings:
        settings_block += f"""      <setting>
        <level>root</level>
        <name>{name}</name>
        <value>{root_values[name]}</value>
      </setting>
"""
    for sid, _ in sections_list:
        settings_block += f"""      <setting>
        <level>section</level>
        <section>section_{sid}</section>
        <name>section_{sid}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>section</level>
        <section>section_{sid}</section>
        <name>section_{sid}_userinfo</name>
        <value>0</value>
      </setting>
"""
    settings_block += f"""      <setting>
        <level>activity</level>
        <activity>forum_{FORUM_MODULEID}</activity>
        <name>forum_{FORUM_MODULEID}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>forum_{FORUM_MODULEID}</activity>
        <name>forum_{FORUM_MODULEID}_userinfo</name>
        <value>0</value>
      </setting>
"""
    for t in temas:
        n = t["n"]
        settings_block += f"""      <setting>
        <level>activity</level>
        <activity>label_{LABEL_MODULEID[n]}</activity>
        <name>label_{LABEL_MODULEID[n]}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>label_{LABEL_MODULEID[n]}</activity>
        <name>label_{LABEL_MODULEID[n]}_userinfo</name>
        <value>0</value>
      </setting>
"""
        for k in range(1, num_practiques_per_tema(t) + 1):
            settings_block += f"""      <setting>
        <level>activity</level>
        <activity>assign_{ASSIGN_MODULEID[n][k]}</activity>
        <name>assign_{ASSIGN_MODULEID[n][k]}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>assign_{ASSIGN_MODULEID[n][k]}</activity>
        <name>assign_{ASSIGN_MODULEID[n][k]}_userinfo</name>
        <value>0</value>
      </setting>
"""
        if NEW_MODE:
            for ridx, (_ce, _crit, _slot, tipo, _nom, _fitxer) in enumerate(recurso_items_full(t)):
                modname = "h5pactivity" if tipo == "h5p" else "scorm"
                rmid = RECURSO_MODULEID[(n, ridx)]
                settings_block += f"""      <setting>
        <level>activity</level>
        <activity>{modname}_{rmid}</activity>
        <name>{modname}_{rmid}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>{modname}_{rmid}</activity>
        <name>{modname}_{rmid}_userinfo</name>
        <value>0</value>
      </setting>
"""
    for ev in EVALS_AMB_EXAMEN:
        settings_block += f"""      <setting>
        <level>activity</level>
        <activity>label_{EXAM_LABEL_MODULEID[ev]}</activity>
        <name>label_{EXAM_LABEL_MODULEID[ev]}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>label_{EXAM_LABEL_MODULEID[ev]}</activity>
        <name>label_{EXAM_LABEL_MODULEID[ev]}_userinfo</name>
        <value>0</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>assign_{EXAM_ASSIGN_MODULEID[ev]}</activity>
        <name>assign_{EXAM_ASSIGN_MODULEID[ev]}_included</name>
        <value>1</value>
      </setting>
      <setting>
        <level>activity</level>
        <activity>assign_{EXAM_ASSIGN_MODULEID[ev]}</activity>
        <name>assign_{EXAM_ASSIGN_MODULEID[ev]}_userinfo</name>
        <value>0</value>
      </setting>
"""
    settings_block += "    </settings>\n"

    moodle_backup_xml = XML_HEADER + f"""<moodle_backup>
  <information>
    <name>{filename}</name>
    <moodle_version>{moodle_version}</moodle_version>
    <moodle_release>{moodle_release}</moodle_release>
    <backup_version>2024100700</backup_version>
    <backup_release>4.5</backup_release>
    <backup_date>{NOW}</backup_date>
    <mnet_remoteusers>0</mnet_remoteusers>
    <include_files>1</include_files>
    <include_file_references_to_external_content>0</include_file_references_to_external_content>
    <original_wwwroot>{wwwroot}</original_wwwroot>
    <original_site_identifier_hash>{site_hash}</original_site_identifier_hash>
    <original_course_id>{COURSE_ID}</original_course_id>
    <original_course_format>topics</original_course_format>
    <original_course_fullname>{course_fullname}</original_course_fullname>
    <original_course_shortname>{course_shortname}</original_course_shortname>
    <original_course_startdate>{NOW}</original_course_startdate>
    <original_course_enddate>{NOW + 31536000}</original_course_enddate>
    <original_course_contextid>{COURSE_CTX}</original_course_contextid>
    <original_system_contextid>1</original_system_contextid>
    <details>
      <detail backup_id="{uuid.uuid4().hex}">
        <type>course</type>
        <format>moodle2</format>
        <interactive>1</interactive>
        <mode>10</mode>
        <execution>1</execution>
        <executiontime>0</executiontime>
      </detail>
    </details>
    <contents>
{activities_block}{sections_block}{course_block}    </contents>
{settings_block}  </information>
</moodle_backup>
"""
    write("moodle_backup.xml", moodle_backup_xml)
    write("moodle_backup.log", "")

    # ============================================================
    # course/*
    # ============================================================
    write("course/course.xml", XML_HEADER + f"""<course id="{COURSE_ID}" contextid="{COURSE_CTX}">
  <shortname>{course_shortname}</shortname>
  <fullname>{course_fullname}</fullname>
  <idnumber></idnumber>
  <summary>{xml_text_escape(course_summary)}</summary>
  <summaryformat>1</summaryformat>
  <format>topics</format>
  <showgrades>1</showgrades>
  <newsitems>5</newsitems>
  <startdate>{NOW}</startdate>
  <enddate>{NOW + 31536000}</enddate>
  <marker>0</marker>
  <maxbytes>52428800</maxbytes>
  <legacyfiles>0</legacyfiles>
  <showreports>0</showreports>
  <visible>1</visible>
  <groupmode>0</groupmode>
  <groupmodeforce>0</groupmodeforce>
  <defaultgroupingid>0</defaultgroupingid>
  <lang></lang>
  <theme></theme>
  <timecreated>{NOW}</timecreated>
  <timemodified>{NOW}</timemodified>
  <requested>0</requested>
  <showactivitydates>1</showactivitydates>
  <showcompletionconditions>1</showcompletionconditions>
  <pdfexportfont>$@NULL@$</pdfexportfont>
  <enablecompletion>1</enablecompletion>
  <completionnotify>0</completionnotify>
  <category id="1">
    <name>{xml_text_escape(data.get("category_name", "Miscel·lania"))}</name>
    <description></description>
  </category>
  <tags>
  </tags>
  <customfields>
  </customfields>
  <courseformatoptions>
    <courseformatoption>
      <format>topics</format>
      <sectionid>0</sectionid>
      <name>hiddensections</name>
      <value>0</value>
    </courseformatoption>
    <courseformatoption>
      <format>topics</format>
      <sectionid>0</sectionid>
      <name>coursedisplay</name>
      <value>0</value>
    </courseformatoption>
  </courseformatoptions>
</course>
""")
    write("course/inforef.xml", XML_HEADER + "<inforef>\n  <roleref>\n    <role>\n      <id>5</id>\n    </role>\n  </roleref>\n</inforef>\n")
    write("course/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
    write("course/enrolments.xml", XML_HEADER + f"""<enrolments>
  <enrols>
    <enrol id="900901">
      <enrol>{data.get("enrol_method", "manual")}</enrol>
      <status>0</status>
      <name>$@NULL@$</name>
      <enrolperiod>0</enrolperiod>
      <enrolstartdate>0</enrolstartdate>
      <enrolenddate>0</enrolenddate>
      <expirynotify>0</expirynotify>
      <expirythreshold>86400</expirythreshold>
      <notifyall>0</notifyall>
      <password>$@NULL@$</password>
      <cost>$@NULL@$</cost>
      <currency>$@NULL@$</currency>
      <roleid>5</roleid>
      <customint1>$@NULL@$</customint1>
      <customint2>$@NULL@$</customint2>
      <customint3>$@NULL@$</customint3>
      <customint4>$@NULL@$</customint4>
      <customint5>$@NULL@$</customint5>
      <customint6>$@NULL@$</customint6>
      <customint7>$@NULL@$</customint7>
      <customint8>$@NULL@$</customint8>
      <customchar1>$@NULL@$</customchar1>
      <customchar2>$@NULL@$</customchar2>
      <customchar3>$@NULL@$</customchar3>
      <customdec1>$@NULL@$</customdec1>
      <customdec2>$@NULL@$</customdec2>
      <customtext1>$@NULL@$</customtext1>
      <customtext2>$@NULL@$</customtext2>
      <customtext3>$@NULL@$</customtext3>
      <customtext4>$@NULL@$</customtext4>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <user_enrolments>
      </user_enrolments>
    </enrol>
  </enrols>
</enrolments>
""")
    write("course/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
    write("course/calendar.xml", XML_HEADER + "<events>\n</events>\n")
    write("course/completiondefaults.xml", XML_HEADER + "<course_completion_defaults>\n</course_completion_defaults>\n")
    write("course/contentbank.xml", XML_HEADER + "<contents>\n</contents>\n")

    # ============================================================
    # sections/*
    # ============================================================
    write(f"sections/section_{SEC_GENERAL}/section.xml", XML_HEADER + f"""<section id="{SEC_GENERAL}">
  <number>0</number>
  <name>$@NULL@$</name>
  <summary>{xml_text_escape(general_intro_html)}</summary>
  <summaryformat>1</summaryformat>
  <sequence>{FORUM_MODULEID}</sequence>
  <visible>1</visible>
  <availabilityjson>$@NULL@$</availabilityjson>
  <component>$@NULL@$</component>
  <itemid>$@NULL@$</itemid>
  <timemodified>{NOW}</timemodified>
</section>
""")

    for t in temas:
        n = t["n"]
        sid = SEC_TEMA[n]
        section_name = t.get("section_name", f"Tema {n}. {t['name']}")

        summary_html = ""
        seq_ids = [str(LABEL_MODULEID[n])] + [str(ASSIGN_MODULEID[n][k]) for k in range(1, num_practiques_per_tema(t) + 1)]
        if NEW_MODE:
            seq_ids += [str(RECURSO_MODULEID[(n, ridx)]) for ridx in range(len(recurso_items_full(t)))]
        sequence = ",".join(seq_ids)

        write(f"sections/section_{sid}/section.xml", XML_HEADER + f"""<section id="{sid}">
  <number>{POSITION[("tema", n)]}</number>
  <name>{xml_text_escape(section_name)}</name>
  <summary>{summary_html}</summary>
  <summaryformat>1</summaryformat>
  <sequence>{sequence}</sequence>
  <visible>1</visible>
  <availabilityjson>$@NULL@$</availabilityjson>
  <component>$@NULL@$</component>
  <itemid>$@NULL@$</itemid>
  <timemodified>{NOW}</timemodified>
</section>
""")

    for ev in EVALS_AMB_EXAMEN:
        sid = SEC_EXAMEN[ev]
        ev_name = eval_names.get(str(ev), f"{ev}a Avaluacio")
        seq_ids = [str(EXAM_LABEL_MODULEID[ev]), str(EXAM_ASSIGN_MODULEID[ev])]
        sequence = ",".join(seq_ids)
        write(f"sections/section_{sid}/section.xml", XML_HEADER + f"""<section id="{sid}">
  <number>{POSITION[("examen", ev)]}</number>
  <name>{xml_text_escape(f"Examen {ev_name}")}</name>
  <summary></summary>
  <summaryformat>1</summaryformat>
  <sequence>{sequence}</sequence>
  <visible>1</visible>
  <availabilityjson>$@NULL@$</availabilityjson>
  <component>$@NULL@$</component>
  <itemid>$@NULL@$</itemid>
  <timemodified>{NOW}</timemodified>
</section>
""")

    # ============================================================
    # activities/forum_XXXX ("Anuncis")
    # ============================================================
    FDIR = f"activities/forum_{FORUM_MODULEID}"
    write(f"{FDIR}/module.xml", XML_HEADER + f"""<module id="{FORUM_MODULEID}" version="2024100700">
  <modulename>forum</modulename>
  <sectionid>{SEC_GENERAL}</sectionid>
  <sectionnumber>0</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>1</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>1</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")
    write(f"{FDIR}/forum.xml", XML_HEADER + f"""<activity id="{FORUM_INSTANCEID}" moduleid="{FORUM_MODULEID}" modulename="forum" contextid="{FORUM_CTX}">
  <forum id="{FORUM_INSTANCEID}">
    <type>news</type>
    <name>Anuncis</name>
    <intro>Anuncis i noticies generals del curs</intro>
    <introformat>1</introformat>
    <duedate>0</duedate>
    <cutoffdate>0</cutoffdate>
    <assessed>0</assessed>
    <assesstimestart>0</assesstimestart>
    <assesstimefinish>0</assesstimefinish>
    <scale>0</scale>
    <maxbytes>0</maxbytes>
    <maxattachments>1</maxattachments>
    <forcesubscribe>1</forcesubscribe>
    <trackingtype>1</trackingtype>
    <rsstype>0</rsstype>
    <rssarticles>0</rssarticles>
    <timemodified>{NOW}</timemodified>
    <warnafter>0</warnafter>
    <blockafter>0</blockafter>
    <blockperiod>0</blockperiod>
    <completiondiscussions>0</completiondiscussions>
    <completionreplies>0</completionreplies>
    <completionposts>0</completionposts>
    <displaywordcount>0</displaywordcount>
    <lockdiscussionafter>0</lockdiscussionafter>
    <grade_forum>0</grade_forum>
    <discussions>
    </discussions>
    <subscriptions>
    </subscriptions>
    <digests>
    </digests>
    <readposts>
    </readposts>
    <trackedprefs>
    </trackedprefs>
    <poststags>
    </poststags>
    <grades>
    </grades>
  </forum>
</activity>
""")
    write(f"{FDIR}/inforef.xml", XML_HEADER + "<inforef>\n</inforef>\n")
    write(f"{FDIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
    write(f"{FDIR}/grading.xml", XML_HEADER + "<areas>\n</areas>\n")
    write(f"{FDIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
    write(f"{FDIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
    write(f"{FDIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")
    write(f"{FDIR}/grades.xml", XML_HEADER + "<activity_gradebook>\n  <grade_items>\n  </grade_items>\n  <grade_letters>\n  </grade_letters>\n</activity_gradebook>\n")

    # ============================================================
    # activities/label_XXXX (notes del professorat, OCULTES a l'alumnat)
    # ============================================================
    comp_by_codigo = {c["codigo"]: c for c in competencias}

    def text_criteri(codi_ce, crit_codi):
        full = comp_by_codigo.get(codi_ce, {})
        for c in full.get("criterios", []):
            if c["codigo"] == crit_codi:
                return c["texto"]
        return ""

    def criteris_desglossats_html(codi):
        full = comp_by_codigo.get(codi)
        if not full or not full.get("criterios"):
            return ""
        items = "".join(
            f"<li><strong>{crit['codigo']}.</strong> {crit['texto']}</li>"
            for crit in full["criterios"]
        )
        return f"<p><strong>Criteris d'avaluacio de {codi}:</strong></p><ul>{items}</ul>"

    # ---- Rubrica d'avaluacio (gradingform_rubric) per a cada Tasca ----
    # 4 nivells de logro fixos, alineats amb les bandes de qualificacio 0-10
    # (No assolit / Assoliment suficient / Assoliment notable / Assoliment
    # excel·lent). Es genera una fila (rubric_criterion) per cada criteri
    # que avalua la practica (1 o 2, segons criteris_per_practica_k), amb
    # el text literal del criteri incrustat en cada nivell. Moodle escala
    # la puntuacio obtinguda a la nota sobre 10 de forma proporcional,
    # independentment de quants criteris tinga la rubrica.
    # NOTA: esquema de gradingform_rubric NO verificat contra una
    # restauracio real (igual que mod_assign).
    # Esquema CONFIRMAT contra una copia de seguretat real d'una sola
    # activitat que Jose va exportar despres de crear una rubrica a ma en
    # una Tasca real d'Aules (Moodle 4.5). Abans d'aquesta confirmacio,
    # dos intents (amb <rubric_criteria>/<rubric_criterion>/<rubric_level>,
    # amb <component> a l'area, i amb <copiedfromid> a la definicio)
    # fallaven: Aules mostrava la rubrica seleccionada pero buida, sense
    # cap criteri. La copia real de Jose va revelar que Moodle empra noms
    # curts (<criteria>/<criterion>/<levels>/<level>, NO amb prefix
    # "rubric_"), que <instances> va DINS de <definition> (no com a
    # germa de <definitions> dins de <area>), que <area> NO porta
    # <component>, que <definition> NO porta <copiedfromid>, i que
    # <options> conte cadenes ("1") en compte d'enters (1). Vore
    # còpia_de_seguretat-moodle2-activity-4365221-assign4365221-*.mbz
    # (aportada per Jose) com a referencia si cal tornar a depurar aixo.
    #
    # 5 nivells de logro (0-4), seguint la mateixa plantilla que Jose ja
    # va provar a ma (Insuficient/Suficient/Be/Notable/Excel·lent), en
    # compte dels 4 nivells (0-3) de la primera versio.
    RUBRIC_LEVELS = [
        (0, "Insuficient",
         "No aplica, o aplica de forma molt incompleta o incorrecta, el criteri: {crit}."),
        (1, "Suficient",
         "Aplica el criteri de forma basica, amb errors rellevants o necessitant ajuda constant: {crit}."),
        (2, "Be",
         "Aplica el criteri de forma majoritariament correcta, amb alguna ajuda puntual o errors menors: {crit}."),
        (3, "Notable",
         "Aplica el criteri correctament i de forma autonoma, amb alguna carencia molt menor: {crit}."),
        (4, "Excel·lent",
         "Aplica el criteri de forma autonoma, correcta i completa, aportant justificacio o reflexio propia: {crit}."),
    ]

    def rubric_criterion_xml(codi_ce, crit_codi, sortorder):
        crit_text = text_criteri(codi_ce, crit_codi) or crit_codi
        levels_xml = ""
        for score, nom_nivell, template in RUBRIC_LEVELS:
            definition = xml_text_escape(
                f"{nom_nivell} ({score}/4). {template.format(crit=crit_text)}"
            )
            levels_xml += f"""                <level id="{next_id()}">
                  <score>{score}.00000</score>
                  <definition>{definition}</definition>
                  <definitionformat>0</definitionformat>
                </level>
"""
        desc = xml_text_escape(f"Criteri {crit_codi}: {crit_text}")
        return f"""            <criterion id="{next_id()}">
              <sortorder>{sortorder}</sortorder>
              <description>{desc}</description>
              <descriptionformat>0</descriptionformat>
              <levels>
{levels_xml}              </levels>
            </criterion>
"""

    def rubric_grading_xml(codi_ce, crits_k, nom_tasca):
        if not crits_k:
            return "<areas>\n</areas>\n"
        area_id = next_id()
        def_id = next_id()
        criteria_xml = "".join(
            rubric_criterion_xml(codi_ce, crit_codi, i + 1)
            for i, crit_codi in enumerate(crits_k)
        )
        options = (
            '{"sortlevelsasc":"1","lockzeropoints":"1","alwaysshowdefinition":"1",'
            '"showdescriptionteacher":"1","showdescriptionstudent":"1",'
            '"showscoreteacher":"1","showscorestudent":"1",'
            '"enableremarks":"1","showremarksstudent":"1"}'
        )
        rubric_name = xml_text_escape(f"Rubrica {nom_tasca}")
        return f"""<areas>
  <area id="{area_id}">
    <areaname>submissions</areaname>
    <activemethod>rubric</activemethod>
    <definitions>
      <definition id="{def_id}">
        <method>rubric</method>
        <name>{rubric_name}</name>
        <description></description>
        <descriptionformat>1</descriptionformat>
        <status>20</status>
        <timecreated>{NOW}</timecreated>
        <timemodified>{NOW}</timemodified>
        <options>{options}</options>
        <plugin_gradingform_rubric_definition>
          <criteria>
{criteria_xml}          </criteria>
        </plugin_gradingform_rubric_definition>
        <instances>
        </instances>
      </definition>
    </definitions>
  </area>
</areas>
"""

    for t in temas:
        n = t["n"]
        sid = SEC_TEMA[n]
        mid = LABEL_MODULEID[n]
        iid = LABEL_INSTANCEID[n]
        ctx = LABEL_CTX[n]
        ev = t["eval"]
        ev_name = eval_names.get(str(ev), f"{ev}a Avaluacio")
        comp = t.get("competencia") or {}
        codi_ce = comp.get("codi", "")

        parts = [f"<p><strong>{ev_name}</strong>"]
        if t.get("bloc"):
            parts[0] += f" — {t['bloc']}"
        parts[0] += "</p>"

        if comp:
            codi = comp.get("codi", "")
            parts.append(
                f"<p><strong>Competencia principal: {codi}</strong><br/>"
                f"{comp.get('text', '')}</p>"
            )
            desglos = criteris_desglossats_html(codi)
            parts.append(desglos if desglos else f"<p>Criteris d'avaluacio: {comp.get('criteris', '')}</p>")
        for extra in t.get("competencies_extra", []):
            codi_extra = extra.get("codi", "")
            parts.append(
                f"<p><strong>{extra.get('etiqueta', 'Competencia relacionada')}: "
                f"{codi_extra}</strong><br/>"
                f"{extra.get('text', '')}</p>"
            )
            desglos_extra = criteris_desglossats_html(codi_extra)
            parts.append(desglos_extra if desglos_extra else f"<p>Criteris d'avaluacio: {extra.get('criteris', '')}</p>")

        tema_offset_prac = TEMA_INDEX_PER_CE.get(n, 0)
        mapping_items = ""
        for k in range(1, num_practiques_per_tema(t) + 1):
            if NEW_MODE:
                ce_k, crit_k = practica_ce_crit(t, k)
                crits_k = [crit_k] if crit_k else []
            else:
                ce_k = codi_ce
                crits_k = criteris_per_practica_k(codi_ce, k, tema_offset_prac)
            if not crits_k:
                detall = "(sense criteri)"
            else:
                detall = ", ".join(
                    f"<strong>{c}</strong>" + (f" ({text_criteri(ce_k, c)})" if text_criteri(ce_k, c) else "")
                    for c in crits_k
                )
            mapping_items += f"<li><strong>Practica Tema#{n} #{k}</strong> -> {detall}</li>"
        _crit_word = "criteri" if CRITERIS_PER_PRACTICA == 1 else "criteris"
        parts.append(
            f"<p><em>Qualificacio per criteris concrets ({CRITERIS_PER_PRACTICA} {_crit_word} per practica):</em></p>"
            f"<ul>{mapping_items}</ul>"
        )
        if CRITERIS_PER_PRACTICA >= 2:
            parts.append(
                "<p><em>El PRIMER criteri de cada parella es la categoria oficial "
                "de la Tasca al Calificador. El SEGON criteri es un element de "
                "qualificacio manual amb el mateix nom, dins de la categoria "
                "d'eixe criteri: despres de corregir el mateix treball, "
                "introdueix alli la nota corresponent a eixe segon criteri.</em></p>"
            )
        else:
            parts.append(
                "<p><em>Cada practica es directament la categoria oficial "
                "(grade_item) del seu criteri al Calificador — no calen "
                "elements manuals addicionals per a les practiques en aquest "
                "mode.</em></p>"
            )

        if NEW_MODE:
            recurs_mapping_items = ""
            for slot_index, (ce_rec, crit_rec, _slot, tipo, nom, _fitxer) in enumerate(recurso_items_full(t)):
                if crit_rec:
                    detall = f"<strong>{crit_rec}</strong>" + (
                        f" ({text_criteri(ce_rec, crit_rec)})" if text_criteri(ce_rec, crit_rec) else ""
                    )
                else:
                    detall = "(sense criteri)"
                recurs_mapping_items += f"<li><strong>Recurs Tema#{n} — {nom}</strong> -> {detall}</li>"
            parts.append(
                "<p><em>Els Recursos avaluen tambe un criteri concret cada un "
                "(igual que les practiques), dins de la subcategoria "
                "\"Recursos\" del seu criteri:</em></p>"
                f"<ul>{recurs_mapping_items}</ul>"
            )

        # Copia de seguretat en text pla de cada rubrica: si per qualsevol
        # motiu la rubrica de Moodle (gradingform_rubric) no es restaura
        # correctament, el professorat pot crear-la a ma en un minut
        # copiant aquest text a "Edita la tasca > Metode de qualificacio
        # avancada > Rubrica".
        rubric_fallback_items = ""
        for k in range(1, num_practiques_per_tema(t) + 1):
            if NEW_MODE:
                ce_k, crit_k = practica_ce_crit(t, k)
                crits_k = [crit_k] if crit_k else []
            else:
                ce_k = codi_ce
                crits_k = criteris_per_practica_k(codi_ce, k, tema_offset_prac)
            if not crits_k:
                continue
            crit_blocks = ""
            for crit_codi in crits_k:
                crit_text = text_criteri(ce_k, crit_codi) or crit_codi
                level_rows = "".join(
                    f"<li><strong>{nom_nivell} ({score}/3):</strong> {template.format(crit=crit_text)}</li>"
                    for score, nom_nivell, template in RUBRIC_LEVELS
                )
                crit_blocks += f"<p><strong>Criteri {crit_codi}</strong></p><ul>{level_rows}</ul>"
            rubric_fallback_items += (
                f"<details><summary>Rubrica de Practica Tema#{n} #{k} "
                "(copia per si cal crear-la a ma a Aules)</summary>"
                f"{crit_blocks}</details>"
            )
        if rubric_fallback_items:
            parts.append(
                "<p><em>Cada practica ja porta configurada una rubrica de "
                "correccio a Aules. Si en restaurar no apareix correctament "
                "(rubrica buida o sense criteris), ací tens el text complet "
                "de cada rubrica per a crear-la a ma (Edita la tasca &gt; "
                "Metode de qualificacio avancada &gt; Rubrica):</em></p>"
                + rubric_fallback_items
            )

        label_intro_html = "".join(parts)

        LDIR = f"activities/label_{mid}"
        write(f"{LDIR}/module.xml", XML_HEADER + f"""<module id="{mid}" version="2024100700">
  <modulename>label</modulename>
  <sectionid>{sid}</sectionid>
  <sectionnumber>{n}</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>0</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>0</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")
        write(f"{LDIR}/label.xml", XML_HEADER + f"""<activity id="{iid}" moduleid="{mid}" modulename="label" contextid="{ctx}">
  <label id="{iid}">
    <name>Notes del professorat — Tema {n}</name>
    <intro>{xml_text_escape(label_intro_html)}</intro>
    <introformat>1</introformat>
    <timemodified>{NOW}</timemodified>
  </label>
</activity>
""")
        write(f"{LDIR}/inforef.xml", XML_HEADER + "<inforef>\n</inforef>\n")
        write(f"{LDIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
        write(f"{LDIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
        write(f"{LDIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
        write(f"{LDIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")
        write(f"{LDIR}/grades.xml", XML_HEADER + "<activity_gradebook>\n  <grade_items>\n  </grade_items>\n  <grade_letters>\n  </grade_letters>\n</activity_gradebook>\n")

    # ============================================================
    # activities/assign_XXXX ("Practica Tema#n #k" — Tasca publica avaluable)
    # El PRIMER criteri del parell es la categoria oficial del grade_item
    # (itemtype=mod) de la propia Tasca. Els elements manuals pel SEGON
    # criteri es generen mes avant, junt amb el gradebook.xml.
    # NOTA: esquema de mod_assign NO verificat contra una restauracio real
    # (a diferencia de label/forum/gradebook).
    # ============================================================
    assign_intro_html = (
        "<p>Tasca de practiques d'aquest tema. El professorat concretara ací "
        "l'enunciat quan corresponga.</p>"
    )
    # guardem, per a cada practica, el segon criteri (si n'hi ha) per a
    # generar despres l'element de qualificacio manual corresponent
    manual_items_pendents = []  # llista de (nom_tasca, categoria_id)

    for t in temas:
        n = t["n"]
        sid = SEC_TEMA[n]

        comp = t.get("competencia") or {}
        codi_ce = comp.get("codi", "")
        tema_offset_prac = TEMA_INDEX_PER_CE.get(n, 0)

        for k in range(1, num_practiques_per_tema(t) + 1):
            mid = ASSIGN_MODULEID[n][k]
            iid = ASSIGN_INSTANCEID[n][k]
            ctx = ASSIGN_CTX[n][k]
            nom_tasca = f"Practica Tema#{n} #{k}"
            if NEW_MODE:
                ce_k, crit_k = practica_ce_crit(t, k)
                crits_k = [crit_k] if crit_k else []
                categoryid = CAT_CRIT.get((t["eval"], ce_k, crit_k), "$@NULL@$") if ce_k else "$@NULL@$"
                rubric_ce = ce_k
            else:
                cats_k = categories_per_practica(codi_ce, k, ev=t["eval"], tema_offset=tema_offset_prac)
                crits_k = criteris_per_practica_k(codi_ce, k, tema_offset_prac)
                categoryid = cats_k[0] if len(cats_k) >= 1 else "$@NULL@$"
                if len(cats_k) >= 2:
                    manual_items_pendents.append((nom_tasca, cats_k[1]))
                rubric_ce = codi_ce

            ADIR = f"activities/assign_{mid}"
            write(f"{ADIR}/module.xml", XML_HEADER + f"""<module id="{mid}" version="2024100700">
  <modulename>assign</modulename>
  <sectionid>{sid}</sectionid>
  <sectionnumber>{n}</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>1</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>1</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")
            write(f"{ADIR}/assign.xml", XML_HEADER + f"""<activity id="{iid}" moduleid="{mid}" modulename="assign" contextid="{ctx}">
  <assign id="{iid}">
    <name>{xml_text_escape(nom_tasca)}</name>
    <intro>{xml_text_escape(assign_intro_html)}</intro>
    <introformat>1</introformat>
    <alwaysshowdescription>1</alwaysshowdescription>
    <submissiondrafts>0</submissiondrafts>
    <sendnotifications>0</sendnotifications>
    <sendlatenotifications>0</sendlatenotifications>
    <sendstudentnotifications>1</sendstudentnotifications>
    <duedate>0</duedate>
    <allowsubmissionsfromdate>0</allowsubmissionsfromdate>
    <grade>10</grade>
    <timemodified>{NOW}</timemodified>
    <requiresubmissionstatement>0</requiresubmissionstatement>
    <completionsubmit>0</completionsubmit>
    <cutoffdate>0</cutoffdate>
    <gradingduedate>0</gradingduedate>
    <teamsubmission>0</teamsubmission>
    <requireallteammemberssubmit>0</requireallteammemberssubmit>
    <teamsubmissiongroupingid>0</teamsubmissiongroupingid>
    <blindmarking>0</blindmarking>
    <hidegrader>0</hidegrader>
    <revealidentities>0</revealidentities>
    <attemptreopenmethod>none</attemptreopenmethod>
    <maxattempts>-1</maxattempts>
    <markingworkflow>0</markingworkflow>
    <markingallocation>0</markingallocation>
    <preventsubmissionnotingroup>0</preventsubmissionnotingroup>
    <submission_plugins>
      <submission_plugin>
        <plugin>onlinetext</plugin>
        <subtype>assignsubmission</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>onlinetext</plugin>
            <subtype>assignsubmission</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
        </config_fields>
      </submission_plugin>
      <submission_plugin>
        <plugin>file</plugin>
        <subtype>assignsubmission</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>maxfilesubmissions</name>
            <value>1</value>
          </config_field>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>maxsubmissionsizebytes</name>
            <value>10485760</value>
          </config_field>
        </config_fields>
      </submission_plugin>
    </submission_plugins>
    <feedback_plugins>
      <feedback_plugin>
        <plugin>comments</plugin>
        <subtype>assignfeedback</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>comments</plugin>
            <subtype>assignfeedback</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
        </config_fields>
      </feedback_plugin>
    </feedback_plugins>
    <submissions>
    </submissions>
    <grades>
    </grades>
    <overrides>
    </overrides>
  </assign>
</activity>
""")
            write(f"{ADIR}/inforef.xml", XML_HEADER + "<inforef>\n</inforef>\n")
            write(f"{ADIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
            write(f"{ADIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
            write(f"{ADIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
            write(f"{ADIR}/grading.xml", XML_HEADER + rubric_grading_xml(rubric_ce, crits_k, nom_tasca))
            write(f"{ADIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")
            write(f"{ADIR}/grades.xml", XML_HEADER + f"""<activity_gradebook>
  <grade_items>
    <grade_item id="{next_id()}">
      <categoryid>{categoryid}</categoryid>
      <itemname>$@NULL@$</itemname>
      <itemtype>mod</itemtype>
      <itemmodule>assign</itemmodule>
      <iteminstance>{iid}</iteminstance>
      <itemnumber>0</itemnumber>
      <iteminfo>$@NULL@$</iteminfo>
      <idnumber>$@NULL@$</idnumber>
      <calculation>$@NULL@$</calculation>
      <gradetype>1</gradetype>
      <grademax>10.00000</grademax>
      <grademin>0.00000</grademin>
      <scaleid>$@NULL@$</scaleid>
      <outcomeid>$@NULL@$</outcomeid>
      <gradepass>0.00000</gradepass>
      <multfactor>1.00000</multfactor>
      <plusfactor>0.00000</plusfactor>
      <aggregationcoef>1.00000</aggregationcoef>
      <aggregationcoef2>0.00000</aggregationcoef2>
      <weightoverride>1</weightoverride>
      <sortorder>{k}</sortorder>
      <display>0</display>
      <decimals>$@NULL@$</decimals>
      <hidden>0</hidden>
      <locked>0</locked>
      <locktime>0</locktime>
      <needsupdate>0</needsupdate>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <grade_grades>
      </grade_grades>
    </grade_item>
  </grade_items>
  <grade_letters>
  </grade_letters>
</activity_gradebook>
""")

    # ============================================================
    # activities/h5pactivity_XXXX i activities/scorm_XXXX ("Recurs Tema#n —
    # {joc}", nomes en mode nou): 6 Recursos H5P/SCORM REALS per tema (2
    # SCORM: pasapalabra i ahorcat; 4 H5P: crucigrama, sopa de lletres,
    # omplir buits, arrossegar paraules), cadascun avaluant NOMES la
    # competencia PRINCIPAL del tema (mai la transversal), igual que les
    # Practiques. Esquema CONFIRMAT contra un backup real de curs complet
    # (amb activitats h5pactivity i scorm reals) que Jose va aportar — vore
    # SKILL.md per als detalls exactes.
    #
    # De moment, cada Recurs incrusta un fitxer d'EXEMPLE ja existent a
    # assets_recursos/ (el mateix per a totes les temes d'eixe tipus de
    # joc) — nomes per fixar l'estructura i el llibre de qualificacions;
    # el professorat el substituira mes avant amb el contingut real de
    # cada tema des d'Aules ("Substitueix amb el fitxer").
    # ============================================================
    if NEW_MODE:
        for t in temas:
            n = t["n"]
            sid = SEC_TEMA[n]
            pos = POSITION[("tema", n)]

            for slot_index, (ce_rec, crit_codi_rec, slot, tipo, nom, fitxer) in enumerate(recurso_items_full(t)):
                rec_cat_id = CAT_CRIT_RECURSOS.get((t["eval"], ce_rec, crit_codi_rec), "$@NULL@$")
                mid = RECURSO_MODULEID[(n, slot_index)]
                iid = RECURSO_INSTANCEID[(n, slot_index)]
                ctx = RECURSO_CTX[(n, slot_index)]
                nom_recurs = f"Recurs Tema#{n} — {nom}"
                modname = "h5pactivity" if tipo == "h5p" else "scorm"
                RDIR = f"activities/{modname}_{mid}"

                write(f"{RDIR}/module.xml", XML_HEADER + f"""<module id="{mid}" version="2024100700">
  <modulename>{modname}</modulename>
  <sectionid>{sid}</sectionid>
  <sectionnumber>{pos}</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>1</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>1</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")

                if tipo == "h5p":
                    asset = load_asset(fitxer)
                    dir_fid = add_file_record(ctx, "mod_h5pactivity", "package", ".")
                    real_fid = add_file_record(
                        ctx, "mod_h5pactivity", "package", asset["filename"],
                        content_bytes=asset["bytes"], mimetype="application/zip.h5p",
                        author="JOSE MANUEL SANCHEZ VILCHEZ", license_="unknown",
                    )
                    grade_item_id = next_id()
                    write(f"{RDIR}/h5pactivity.xml", XML_HEADER + f"""<activity id="{iid}" moduleid="{mid}" modulename="h5pactivity" contextid="{ctx}">
  <h5pactivity id="{iid}">
    <name>{xml_text_escape(nom_recurs)}</name>
    <timecreated>{NOW}</timecreated>
    <timemodified>{NOW}</timemodified>
    <intro></intro>
    <introformat>1</introformat>
    <grade>10</grade>
    <displayoptions>0</displayoptions>
    <enabletracking>1</enabletracking>
    <grademethod>1</grademethod>
    <reviewmode>1</reviewmode>
    <attempts>
    </attempts>
  </h5pactivity>
</activity>
""")
                    write(f"{RDIR}/grades.xml", XML_HEADER + f"""<activity_gradebook>
  <grade_items>
    <grade_item id="{grade_item_id}">
      <categoryid>{rec_cat_id}</categoryid>
      <itemname>{xml_text_escape(nom_recurs)}</itemname>
      <itemtype>mod</itemtype>
      <itemmodule>h5pactivity</itemmodule>
      <iteminstance>{iid}</iteminstance>
      <itemnumber>0</itemnumber>
      <iteminfo>$@NULL@$</iteminfo>
      <idnumber>$@NULL@$</idnumber>
      <calculation>$@NULL@$</calculation>
      <gradetype>1</gradetype>
      <grademax>10.00000</grademax>
      <grademin>0.00000</grademin>
      <scaleid>$@NULL@$</scaleid>
      <outcomeid>$@NULL@$</outcomeid>
      <gradepass>0.00000</gradepass>
      <multfactor>1.00000</multfactor>
      <plusfactor>0.00000</plusfactor>
      <aggregationcoef>1.00000</aggregationcoef>
      <aggregationcoef2>0.00000</aggregationcoef2>
      <weightoverride>0</weightoverride>
      <sortorder>1</sortorder>
      <display>0</display>
      <decimals>$@NULL@$</decimals>
      <hidden>0</hidden>
      <locked>0</locked>
      <locktime>0</locktime>
      <needsupdate>0</needsupdate>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <grade_grades>
      </grade_grades>
    </grade_item>
  </grade_items>
  <grade_letters>
  </grade_letters>
</activity_gradebook>
""")
                    write(f"{RDIR}/inforef.xml", XML_HEADER + f"""<inforef>
  <fileref>
    <file>
      <id>{dir_fid}</id>
    </file>
    <file>
      <id>{real_fid}</id>
    </file>
  </fileref>
  <grade_itemref>
    <grade_item>
      <id>{grade_item_id}</id>
    </grade_item>
  </grade_itemref>
</inforef>
""")

                else:  # scorm
                    asset = load_asset(fitxer)
                    inner = scorm_inner_info(fitxer)
                    dir_pkg_fid = add_file_record(ctx, "mod_scorm", "package", ".")
                    zip_fid = add_file_record(
                        ctx, "mod_scorm", "package", asset["filename"],
                        content_bytes=asset["bytes"], mimetype="application/zip",
                        author="JOSE MANUEL SANCHEZ VILCHEZ", license_="unknown",
                    )
                    dir_content_fid = add_file_record(ctx, "mod_scorm", "content", ".")
                    index_fid = add_file_record(
                        ctx, "mod_scorm", "content", "index.html",
                        content_bytes=inner["index_bytes"], mimetype="text/html",
                        source="$@NULL@$",
                    )
                    manifest_fid = add_file_record(
                        ctx, "mod_scorm", "content", "imsmanifest.xml",
                        content_bytes=inner["manifest_bytes"], mimetype="application/xml",
                        source="$@NULL@$",
                    )
                    sco_org_id = next_id()
                    sco_item_id = next_id()
                    sd1, sd2, sd3 = next_id(), next_id(), next_id()
                    grade_item_id = next_id()
                    write(f"{RDIR}/scorm.xml", XML_HEADER + f"""<activity id="{iid}" moduleid="{mid}" modulename="scorm" contextid="{ctx}">
  <scorm id="{iid}">
    <name>{xml_text_escape(nom_recurs)}</name>
    <scormtype>local</scormtype>
    <reference>{xml_text_escape(asset["filename"])}</reference>
    <intro></intro>
    <introformat>1</introformat>
    <version>SCORM_1.2</version>
    <maxgrade>100</maxgrade>
    <grademethod>1</grademethod>
    <whatgrade>0</whatgrade>
    <maxattempt>0</maxattempt>
    <forcecompleted>0</forcecompleted>
    <forcenewattempt>0</forcenewattempt>
    <lastattemptlock>0</lastattemptlock>
    <masteryoverride>1</masteryoverride>
    <displayattemptstatus>1</displayattemptstatus>
    <displaycoursestructure>0</displaycoursestructure>
    <updatefreq>0</updatefreq>
    <sha1hash>{asset["sha1"]}</sha1hash>
    <md5hash></md5hash>
    <revision>1</revision>
    <launch>{sco_item_id}</launch>
    <skipview>0</skipview>
    <hidebrowse>0</hidebrowse>
    <hidetoc>0</hidetoc>
    <nav>1</nav>
    <navpositionleft>-100</navpositionleft>
    <navpositiontop>-100</navpositiontop>
    <auto>0</auto>
    <popup>0</popup>
    <options></options>
    <width>100</width>
    <height>500</height>
    <timeopen>0</timeopen>
    <timeclose>0</timeclose>
    <timemodified>{NOW}</timemodified>
    <completionstatusrequired>$@NULL@$</completionstatusrequired>
    <completionscorerequired>$@NULL@$</completionscorerequired>
    <completionstatusallscos>0</completionstatusallscos>
    <autocommit>0</autocommit>
    <scoes>
      <sco id="{sco_org_id}">
        <manifest>{xml_text_escape(inner["manifest_id"])}</manifest>
        <organization></organization>
        <parent>/</parent>
        <identifier>ORG1</identifier>
        <launch></launch>
        <scormtype></scormtype>
        <title>{xml_text_escape(inner["org_title"])}</title>
        <sortorder>1</sortorder>
        <sco_datas>
        </sco_datas>
        <seq_ruleconds>
        </seq_ruleconds>
        <seq_rolluprules>
        </seq_rolluprules>
        <seq_objectives>
        </seq_objectives>
        <sco_tracks>
        </sco_tracks>
      </sco>
      <sco id="{sco_item_id}">
        <manifest>{xml_text_escape(inner["manifest_id"])}</manifest>
        <organization>ORG1</organization>
        <parent>ORG1</parent>
        <identifier>ITEM1</identifier>
        <launch>index.html</launch>
        <scormtype>sco</scormtype>
        <title>{xml_text_escape(inner["item_title"])}</title>
        <sortorder>2</sortorder>
        <sco_datas>
          <sco_data id="{sd1}">
            <name>isvisible</name>
            <value>true</value>
          </sco_data>
          <sco_data id="{sd2}">
            <name>parameters</name>
            <value></value>
          </sco_data>
          <sco_data id="{sd3}">
            <name>masteryscore</name>
            <value>{xml_text_escape(inner["mastery"])}</value>
          </sco_data>
        </sco_datas>
        <seq_ruleconds>
        </seq_ruleconds>
        <seq_rolluprules>
        </seq_rolluprules>
        <seq_objectives>
        </seq_objectives>
        <sco_tracks>
        </sco_tracks>
      </sco>
    </scoes>
  </scorm>
</activity>
""")
                    write(f"{RDIR}/grades.xml", XML_HEADER + f"""<activity_gradebook>
  <grade_items>
    <grade_item id="{grade_item_id}">
      <categoryid>{rec_cat_id}</categoryid>
      <itemname>{xml_text_escape(nom_recurs)}</itemname>
      <itemtype>mod</itemtype>
      <itemmodule>scorm</itemmodule>
      <iteminstance>{iid}</iteminstance>
      <itemnumber>0</itemnumber>
      <iteminfo>$@NULL@$</iteminfo>
      <idnumber>$@NULL@$</idnumber>
      <calculation>$@NULL@$</calculation>
      <gradetype>1</gradetype>
      <grademax>10.00000</grademax>
      <grademin>0.00000</grademin>
      <scaleid>$@NULL@$</scaleid>
      <outcomeid>$@NULL@$</outcomeid>
      <gradepass>0.00000</gradepass>
      <multfactor>1.00000</multfactor>
      <plusfactor>0.00000</plusfactor>
      <aggregationcoef>1.00000</aggregationcoef>
      <aggregationcoef2>0.00000</aggregationcoef2>
      <weightoverride>0</weightoverride>
      <sortorder>1</sortorder>
      <display>0</display>
      <decimals>$@NULL@$</decimals>
      <hidden>0</hidden>
      <locked>0</locked>
      <locktime>0</locktime>
      <needsupdate>0</needsupdate>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <grade_grades>
      </grade_grades>
    </grade_item>
  </grade_items>
  <grade_letters>
  </grade_letters>
</activity_gradebook>
""")
                    write(f"{RDIR}/inforef.xml", XML_HEADER + f"""<inforef>
  <fileref>
    <file>
      <id>{dir_pkg_fid}</id>
    </file>
    <file>
      <id>{zip_fid}</id>
    </file>
    <file>
      <id>{dir_content_fid}</id>
    </file>
    <file>
      <id>{index_fid}</id>
    </file>
    <file>
      <id>{manifest_fid}</id>
    </file>
  </fileref>
  <grade_itemref>
    <grade_item>
      <id>{grade_item_id}</id>
    </grade_item>
  </grade_itemref>
</inforef>
""")

                write(f"{RDIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
                write(f"{RDIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
                write(f"{RDIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
                write(f"{RDIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")

    # ============================================================
    # activities/label_XXXX + assign_XXXX de cada seccio "Examen" (nomes en
    # mode nou): una Tasca publica per avaluacio que qualifica TOTES les CE
    # (principals i transversals) que apareixen en eixa avaluacio. Com una
    # Tasca nomes pot tindre un unic grade_item real, la PRIMERA CE de la
    # llista es la categoria oficial de la Tasca; la resta son elements de
    # qualificacio manuals dins de la mateixa categoria "Examen".
    # ============================================================
    examen_manual_items_pendents = []  # llista de (nom_item, categoria_id)

    for ev in EVALS_AMB_EXAMEN:
        ev_name = eval_names.get(str(ev), f"{ev}a Avaluacio")
        nom_examen = f"Examen {ev_name}"
        ces = CES_EXAMEN_PER_EVAL.get(ev, [])

        mid_l = EXAM_LABEL_MODULEID[ev]
        iid_l = EXAM_LABEL_INSTANCEID[ev]
        ctx_l = EXAM_LABEL_CTX[ev]
        sid = SEC_EXAMEN[ev]
        pos = POSITION[("examen", ev)]

        mapping_html = ""
        for i, codi in enumerate(ces):
            comp_full = comp_by_codigo.get(codi, {})
            tipus = ("grade_item real de la Tasca (la nota que li poses en corregir-la)"
                     if i == 0 else
                     "element de qualificacio manual (introdueix-la a ma despres de corregir)")
            mapping_html += (
                f"<li><strong>{codi}</strong>"
                + (f" — {comp_full.get('texto', '')}" if comp_full.get("texto") else "")
                + f": {tipus}</li>"
            )
        label_intro_html = (
            f"<p><strong>{ev_name} — Examen</strong></p>"
            "<p>Aquest examen avalua TOTES les competencies especifiques "
            "treballades en aquesta avaluacio (incloses les transversals, "
            "que les practiques i els recursos no qualifiquen directament):</p>"
            f"<ul>{mapping_html}</ul>"
            "<p><em>Cada CE principal te ara la seua propia categoria "
            "\"Examen\" (10% del pes d'eixa CE); les CE transversals "
            "(com CE5) nomes tenen esta branca d'Examen, ja que ni les "
            "practiques ni els recursos les avaluen.</em></p>"
            "<p><em>Cal traure almenys un 5 sobre 10 en aquest examen "
            "perque l'alumnat aprove l'avaluacio, amb independencia de la "
            "mitjana ponderada. Moodle marca este llindar (\"nota per a "
            "aprovar\") en l'element de qualificacio de cada categoria "
            "Examen, pero la mitjana ponderada de Moodle NO aplica este "
            "requisit de forma automatica: si l'alumnat no arriba a 5 en "
            "l'examen, el professorat ha de revisar i, si cal, "
            "anul·lar/ajustar a ma la nota final d'esta avaluacio al "
            "calificador.</em></p>"
        )

        LDIR = f"activities/label_{mid_l}"
        write(f"{LDIR}/module.xml", XML_HEADER + f"""<module id="{mid_l}" version="2024100700">
  <modulename>label</modulename>
  <sectionid>{sid}</sectionid>
  <sectionnumber>{pos}</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>0</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>0</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")
        write(f"{LDIR}/label.xml", XML_HEADER + f"""<activity id="{iid_l}" moduleid="{mid_l}" modulename="label" contextid="{ctx_l}">
  <label id="{iid_l}">
    <name>Notes del professorat — Examen {ev_name}</name>
    <intro>{xml_text_escape(label_intro_html)}</intro>
    <introformat>1</introformat>
    <timemodified>{NOW}</timemodified>
  </label>
</activity>
""")
        write(f"{LDIR}/inforef.xml", XML_HEADER + "<inforef>\n</inforef>\n")
        write(f"{LDIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
        write(f"{LDIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
        write(f"{LDIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
        write(f"{LDIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")
        write(f"{LDIR}/grades.xml", XML_HEADER + "<activity_gradebook>\n  <grade_items>\n  </grade_items>\n  <grade_letters>\n  </grade_letters>\n</activity_gradebook>\n")

        mid_a = EXAM_ASSIGN_MODULEID[ev]
        iid_a = EXAM_ASSIGN_INSTANCEID[ev]
        ctx_a = EXAM_ASSIGN_CTX[ev]
        categoryid_examen = CAT_EXAMEN.get((ev, ces[0])) if ces else "$@NULL@$"
        if categoryid_examen is None:
            categoryid_examen = "$@NULL@$"
        if len(ces) >= 2:
            for codi_extra in ces[1:]:
                cat_extra = CAT_EXAMEN.get((ev, codi_extra))
                if cat_extra is not None:
                    examen_manual_items_pendents.append((f"{nom_examen} ({codi_extra})", cat_extra))

        assign_examen_intro_html = (
            "<p>Examen d'aquesta avaluacio. El professorat hi afegira "
            "l'enunciat concret quan corresponga.</p>"
        )

        ADIR = f"activities/assign_{mid_a}"
        write(f"{ADIR}/module.xml", XML_HEADER + f"""<module id="{mid_a}" version="2024100700">
  <modulename>assign</modulename>
  <sectionid>{sid}</sectionid>
  <sectionnumber>{pos}</sectionnumber>
  <idnumber>$@NULL@$</idnumber>
  <added>{NOW}</added>
  <score>0</score>
  <indent>0</indent>
  <visible>1</visible>
  <visibleoncoursepage>1</visibleoncoursepage>
  <visibleold>1</visibleold>
  <groupmode>0</groupmode>
  <groupingid>0</groupingid>
  <completion>0</completion>
  <completiongradeitemnumber>$@NULL@$</completiongradeitemnumber>
  <completionpassgrade>0</completionpassgrade>
  <completionview>0</completionview>
  <completionexpected>0</completionexpected>
  <availability>$@NULL@$</availability>
  <showdescription>0</showdescription>
  <downloadcontent>1</downloadcontent>
  <lang>$@NULL@$</lang>
  <tags>
  </tags>
</module>
""")
        write(f"{ADIR}/assign.xml", XML_HEADER + f"""<activity id="{iid_a}" moduleid="{mid_a}" modulename="assign" contextid="{ctx_a}">
  <assign id="{iid_a}">
    <name>{xml_text_escape(nom_examen)}</name>
    <intro>{xml_text_escape(assign_examen_intro_html)}</intro>
    <introformat>1</introformat>
    <alwaysshowdescription>1</alwaysshowdescription>
    <submissiondrafts>0</submissiondrafts>
    <sendnotifications>0</sendnotifications>
    <sendlatenotifications>0</sendlatenotifications>
    <sendstudentnotifications>1</sendstudentnotifications>
    <duedate>0</duedate>
    <allowsubmissionsfromdate>0</allowsubmissionsfromdate>
    <grade>10</grade>
    <timemodified>{NOW}</timemodified>
    <requiresubmissionstatement>0</requiresubmissionstatement>
    <completionsubmit>0</completionsubmit>
    <cutoffdate>0</cutoffdate>
    <gradingduedate>0</gradingduedate>
    <teamsubmission>0</teamsubmission>
    <requireallteammemberssubmit>0</requireallteammemberssubmit>
    <teamsubmissiongroupingid>0</teamsubmissiongroupingid>
    <blindmarking>0</blindmarking>
    <hidegrader>0</hidegrader>
    <revealidentities>0</revealidentities>
    <attemptreopenmethod>none</attemptreopenmethod>
    <maxattempts>-1</maxattempts>
    <markingworkflow>0</markingworkflow>
    <markingallocation>0</markingallocation>
    <preventsubmissionnotingroup>0</preventsubmissionnotingroup>
    <submission_plugins>
      <submission_plugin>
        <plugin>onlinetext</plugin>
        <subtype>assignsubmission</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>onlinetext</plugin>
            <subtype>assignsubmission</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
        </config_fields>
      </submission_plugin>
      <submission_plugin>
        <plugin>file</plugin>
        <subtype>assignsubmission</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>maxfilesubmissions</name>
            <value>1</value>
          </config_field>
          <config_field>
            <plugin>file</plugin>
            <subtype>assignsubmission</subtype>
            <name>maxsubmissionsizebytes</name>
            <value>10485760</value>
          </config_field>
        </config_fields>
      </submission_plugin>
    </submission_plugins>
    <feedback_plugins>
      <feedback_plugin>
        <plugin>comments</plugin>
        <subtype>assignfeedback</subtype>
        <version>2024100700</version>
        <config_fields>
          <config_field>
            <plugin>comments</plugin>
            <subtype>assignfeedback</subtype>
            <name>enabled</name>
            <value>1</value>
          </config_field>
        </config_fields>
      </feedback_plugin>
    </feedback_plugins>
    <submissions>
    </submissions>
    <grades>
    </grades>
    <overrides>
    </overrides>
  </assign>
</activity>
""")
        write(f"{ADIR}/inforef.xml", XML_HEADER + "<inforef>\n</inforef>\n")
        write(f"{ADIR}/roles.xml", XML_HEADER + "<roles>\n  <role_overrides>\n  </role_overrides>\n  <role_assignments>\n  </role_assignments>\n</roles>\n")
        write(f"{ADIR}/filters.xml", XML_HEADER + "<filters>\n  <filter_actives>\n  </filter_actives>\n  <filter_configs>\n  </filter_configs>\n</filters>\n")
        write(f"{ADIR}/calendar.xml", XML_HEADER + "<events>\n</events>\n")
        write(f"{ADIR}/grading.xml", XML_HEADER + "<areas>\n</areas>\n")
        write(f"{ADIR}/grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")
        write(f"{ADIR}/grades.xml", XML_HEADER + f"""<activity_gradebook>
  <grade_items>
    <grade_item id="{next_id()}">
      <categoryid>{categoryid_examen}</categoryid>
      <itemname>$@NULL@$</itemname>
      <itemtype>mod</itemtype>
      <itemmodule>assign</itemmodule>
      <iteminstance>{iid_a}</iteminstance>
      <itemnumber>0</itemnumber>
      <iteminfo>$@NULL@$</iteminfo>
      <idnumber>$@NULL@$</idnumber>
      <calculation>$@NULL@$</calculation>
      <gradetype>1</gradetype>
      <grademax>10.00000</grademax>
      <grademin>0.00000</grademin>
      <scaleid>$@NULL@$</scaleid>
      <outcomeid>$@NULL@$</outcomeid>
      <gradepass>0.00000</gradepass>
      <multfactor>1.00000</multfactor>
      <plusfactor>0.00000</plusfactor>
      <aggregationcoef>1.00000</aggregationcoef>
      <aggregationcoef2>0.00000</aggregationcoef2>
      <weightoverride>1</weightoverride>
      <sortorder>1</sortorder>
      <display>0</display>
      <decimals>$@NULL@$</decimals>
      <hidden>0</hidden>
      <locked>0</locked>
      <locktime>0</locktime>
      <needsupdate>0</needsupdate>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <grade_grades>
      </grade_grades>
    </grade_item>
  </grade_items>
  <grade_letters>
  </grade_letters>
</activity_gradebook>
""")

    # ============================================================
    # gradebook.xml
    #  - Mode classic: Curs -> CE -> Criteri.
    #  - Mode nou: Curs -> Avaluacio -> {Activitats -> CE -> Criteri,
    #    Recursos, Examen}, amb pesos w_act/w_rec/w_exa entre les tres
    #    subcategories de cada avaluacio.
    # Inclou tambe els elements de qualificacio MANUALS (2n criteri de cada
    # practica en mode classic; CE addicionals de cada examen en mode nou).
    # ============================================================
    categories = [(CAT_ROOT, "$@NULL@$", 1, f"/{CAT_ROOT}/", "?", 10)]
    if NEW_MODE:
        for ev in sorted(CAT_AVAL):
            aval_id = CAT_AVAL[ev]
            ev_name = eval_names.get(str(ev), f"{ev}a Avaluacio")
            categories.append((aval_id, CAT_ROOT, 2, f"/{CAT_ROOT}/{aval_id}/", ev_name, 10))
            ce_principals = CE_PRINCIPALS_PER_EVAL.get(ev, [])
            for codi in ce_principals:
                ce_id = CAT_CE[(ev, codi)]
                categories.append((ce_id, aval_id, 3, f"/{CAT_ROOT}/{aval_id}/{ce_id}/", codi, 10))
                act_id = CAT_ACTIVITATS[(ev, codi)]
                rec_id = CAT_RECURSOS[(ev, codi)]
                exa_id = CAT_EXAMEN[(ev, codi)]
                categories.append((act_id, ce_id, 4, f"/{CAT_ROOT}/{aval_id}/{ce_id}/{act_id}/", "Activitats", 10))
                categories.append((rec_id, ce_id, 4, f"/{CAT_ROOT}/{aval_id}/{ce_id}/{rec_id}/", "Recursos", 10))
                categories.append((exa_id, ce_id, 4, f"/{CAT_ROOT}/{aval_id}/{ce_id}/{exa_id}/", "Examen", 10))
                for crit_codi in CAT_CRIT_ORDER.get(codi, []):
                    crit_id = CAT_CRIT[(ev, codi, crit_codi)]
                    categories.append((crit_id, act_id, 5,
                                        f"/{CAT_ROOT}/{aval_id}/{ce_id}/{act_id}/{crit_id}/",
                                        crit_codi, 10))
                    crit_rec_id = CAT_CRIT_RECURSOS[(ev, codi, crit_codi)]
                    categories.append((crit_rec_id, rec_id, 5,
                                        f"/{CAT_ROOT}/{aval_id}/{ce_id}/{rec_id}/{crit_rec_id}/",
                                        crit_codi, 10))
            # CE transversals (nomes branca CE -> Examen, sense Activitats
            # ni Recursos: cap practica ni recurs les toca directament)
            for codi in CES_EXAMEN_PER_EVAL.get(ev, []):
                if codi in ce_principals:
                    continue
                ce_id = CAT_CE.get((ev, codi))
                exa_id = CAT_EXAMEN.get((ev, codi))
                if ce_id is None or exa_id is None:
                    continue
                categories.append((ce_id, aval_id, 3, f"/{CAT_ROOT}/{aval_id}/{ce_id}/", codi, 10))
                categories.append((exa_id, ce_id, 4, f"/{CAT_ROOT}/{aval_id}/{ce_id}/{exa_id}/", "Examen", 10))
    else:
        for comp in competencias:
            ce_id = CAT_CE[comp["codigo"]]
            categories.append((ce_id, CAT_ROOT, 2, f"/{CAT_ROOT}/{ce_id}/", comp["codigo"], 10))
        for comp in competencias:
            ce_id = CAT_CE[comp["codigo"]]
            for crit in comp.get("criterios", []):
                crit_id = CAT_CRIT[(comp["codigo"], crit["codigo"])]
                categories.append((crit_id, ce_id, 3, f"/{CAT_ROOT}/{ce_id}/{crit_id}/",
                                    crit["codigo"], 10))

    cat_xml = "  <grade_categories>\n"
    for cid, parent, depth, path, fullname, agg in categories:
        cat_xml += f"""    <grade_category id="{cid}">
      <parent>{parent}</parent>
      <depth>{depth}</depth>
      <path>{path}</path>
      <fullname>{xml_text_escape(fullname)}</fullname>
      <aggregation>{agg}</aggregation>
      <keephigh>0</keephigh>
      <droplow>0</droplow>
      <aggregateonlygraded>1</aggregateonlygraded>
      <aggregateoutcomes>0</aggregateoutcomes>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <hidden>0</hidden>
    </grade_category>
"""
    cat_xml += "  </grade_categories>\n"

    # pesos explicits (distints d'1.0) nomes per a Activitats/Recursos/Examen
    # DINS DE CADA CE (60/30/10 per defecte); la resta (avaluacions entre si,
    # CE entre si, criteris entre si) mantenen pes igual (coef 1.0) com
    # sempre. Les CE transversals (nomes Examen) no necessiten pes explicit
    # perque son l'unic fill de la seua CE.
    explicit_coef = {}
    # nota minima per a aprovar (gradepass), nomes te sentit en la categoria
    # Examen: Moodle la mostra com a indicador visual de aprovat/suspes,
    # pero NO altera per si mateixa el calcul de la mitjana ponderada (vore
    # nota a la etiqueta oculta de cada Examen).
    explicit_gradepass = {}
    if NEW_MODE:
        for key, act_id in CAT_ACTIVITATS.items():
            explicit_coef[act_id] = w_act
        for key, rec_id in CAT_RECURSOS.items():
            explicit_coef[rec_id] = w_rec
        for key, exa_id in CAT_EXAMEN.items():
            if key in CAT_ACTIVITATS:  # nomes les CE amb Activitats tenen pes explicit d'Examen
                explicit_coef[exa_id] = w_exa
            explicit_gradepass[exa_id] = 5.0

    items = [(next_id(), "course", CAT_ROOT, 100.0, 0.0, 0, 1, None, 0.0)]
    sortorder = 2
    for cid, parent, depth, path, fullname, agg in categories:
        if cid == CAT_ROOT:
            continue
        coef = explicit_coef.get(cid, 1.0)
        gradepass = explicit_gradepass.get(cid, 0.0)
        items.append((next_id(), "category", cid, 10.0, coef, 1, sortorder, None, gradepass))
        sortorder += 1
    # elements manuals: 2n criteri de cada practica (mode classic) i/o CE
    # addicionals de cada examen (mode nou). Els Recursos ara son activitats
    # H5P/SCORM reals amb el seu propi grade_item "mod" (es generen junt amb
    # cada activitat, no ací).
    for nom_tasca, cat_id in manual_items_pendents:
        items.append((next_id(), "manual", cat_id, 10.0, 1.0, 1, sortorder,
                      f"{nom_tasca} (2n criteri)", 0.0))
        sortorder += 1
    for nom_examen_item, cat_id in examen_manual_items_pendents:
        items.append((next_id(), "manual", cat_id, 10.0, 1.0, 1, sortorder,
                      nom_examen_item, 0.0))
        sortorder += 1

    items_xml = "  <grade_items>\n"
    for iid, itemtype, iteminstance_or_cat, grademax, coef, override, sort, itemname, gradepass in items:
        if itemtype == "manual":
            categoryid_val = iteminstance_or_cat
            iteminstance_val = "$@NULL@$"
        else:
            categoryid_val = "$@NULL@$"
            iteminstance_val = iteminstance_or_cat
        itemname_val = xml_text_escape(itemname) if itemname else "$@NULL@$"
        items_xml += f"""    <grade_item id="{iid}">
      <categoryid>{categoryid_val}</categoryid>
      <itemname>{itemname_val}</itemname>
      <itemtype>{itemtype}</itemtype>
      <itemmodule>$@NULL@$</itemmodule>
      <iteminstance>{iteminstance_val}</iteminstance>
      <itemnumber>$@NULL@$</itemnumber>
      <iteminfo>$@NULL@$</iteminfo>
      <idnumber>$@NULL@$</idnumber>
      <calculation>$@NULL@$</calculation>
      <gradetype>1</gradetype>
      <grademax>{grademax:.5f}</grademax>
      <grademin>0.00000</grademin>
      <scaleid>$@NULL@$</scaleid>
      <outcomeid>$@NULL@$</outcomeid>
      <gradepass>{gradepass:.5f}</gradepass>
      <multfactor>1.00000</multfactor>
      <plusfactor>0.00000</plusfactor>
      <aggregationcoef>{coef:.5f}</aggregationcoef>
      <aggregationcoef2>0.00000</aggregationcoef2>
      <weightoverride>{override}</weightoverride>
      <sortorder>{sort}</sortorder>
      <display>0</display>
      <decimals>$@NULL@$</decimals>
      <hidden>0</hidden>
      <locked>0</locked>
      <locktime>0</locktime>
      <needsupdate>0</needsupdate>
      <timecreated>{NOW}</timecreated>
      <timemodified>{NOW}</timemodified>
      <grade_grades>
      </grade_grades>
    </grade_item>
"""
    items_xml += "  </grade_items>\n"

    gradebook_xml = XML_HEADER + f"""<gradebook>
  <attributes>
  </attributes>
{cat_xml}{items_xml}  <grade_letters>
  </grade_letters>
  <grade_settings>
    <grade_setting id="">
      <name>minmaxtouse</name>
      <value>1</value>
    </grade_setting>
  </grade_settings>
</gradebook>
"""
    write("gradebook.xml", gradebook_xml)

    # ---- boilerplate raiz (identic a un backup real, sense contingut) ----
    write("badges.xml", XML_HEADER + "<badges>\n</badges>\n")
    write("completion.xml", XML_HEADER + "<course_completion>\n</course_completion>\n")
    write("groups.xml", XML_HEADER + "<groups>\n  <groupcustomfields>\n  </groupcustomfields>\n  <groupings>\n    <groupingcustomfields>\n    </groupingcustomfields>\n  </groupings>\n</groups>\n")
    write("outcomes.xml", XML_HEADER + "<outcomes_definition>\n</outcomes_definition>\n")
    write("questions.xml", XML_HEADER + "<question_categories>\n</question_categories>\n")
    write("roles.xml", XML_HEADER + '<roles_definition>\n  <role id="5">\n    <name></name>\n    <shortname>student</shortname>\n    <nameincourse>$@NULL@$</nameincourse>\n    <description></description>\n    <sortorder>5</sortorder>\n    <archetype>student</archetype>\n  </role>\n</roles_definition>\n')
    write("scales.xml", XML_HEADER + "<scales_definition>\n</scales_definition>\n")
    write("grade_history.xml", XML_HEADER + "<grade_history>\n  <grade_grades>\n  </grade_grades>\n</grade_history>\n")

    files_xml_body = "<files>\n"
    for fr in FILES_REGISTRY:
        files_xml_body += f"""  <file id="{fr['id']}">
    <contenthash>{fr['contenthash']}</contenthash>
    <contextid>{fr['contextid']}</contextid>
    <component>{fr['component']}</component>
    <filearea>{fr['filearea']}</filearea>
    <itemid>{fr['itemid']}</itemid>
    <filepath>{fr['filepath']}</filepath>
    <filename>{xml_text_escape(fr['filename'])}</filename>
    <userid>2</userid>
    <filesize>{fr['filesize']}</filesize>
    <mimetype>{fr['mimetype']}</mimetype>
    <status>0</status>
    <timecreated>{NOW}</timecreated>
    <timemodified>{NOW}</timemodified>
    <source>{xml_text_escape(fr['source']) if fr['source'] != "$@NULL@$" else "$@NULL@$"}</source>
    <author>{fr['author']}</author>
    <license>{fr['license']}</license>
    <sortorder>0</sortorder>
    <repositorytype>$@NULL@$</repositorytype>
    <repositoryid>$@NULL@$</repositoryid>
    <reference>$@NULL@$</reference>
  </file>
"""
    files_xml_body += "</files>\n"
    write("files.xml", XML_HEADER + files_xml_body)

    # bolcar els blobs binaris unics (deduplicats per sha1) a files/<2primers>/<hash>
    for sha1, blob in BLOBS_TO_WRITE.items():
        write_binary(f"files/{sha1[:2]}/{sha1}", blob)

    # ============================================================
    # empaquetar com a .mbz (tar.gz)
    # ============================================================
    with tarfile.open(out_path, "w:gz") as tar:
        for root, dirs, files in os.walk(tmp_dir):
            for fn in files:
                full = os.path.join(root, fn)
                arcname = os.path.relpath(full, tmp_dir)
                tar.add(full, arcname=arcname)

    return tmp_dir, len(sections_list), len(categories)


def validate(out_path):
    vdir = out_path + "_verify"
    if os.path.exists(vdir):
        shutil.rmtree(vdir, ignore_errors=True)
    os.makedirs(vdir, exist_ok=True)
    with tarfile.open(out_path, "r:gz") as tar:
        tar.extractall(vdir)

    ok = True
    xml_files = glob.glob(f"{vdir}/**/*.xml", recursive=True)
    bad_xml = []
    for f in xml_files:
        try:
            minidom.parse(f)
        except Exception as e:
            bad_xml.append((f, str(e)))
    print(f"[validate] Ficheros XML: {len(xml_files)}  invalidos: {len(bad_xml)}")
    for f, e in bad_xml:
        print(f"  INVALIDO: {f} -> {e}")
        ok = False

    bad_summary = 0
    for f in glob.glob(f"{vdir}/sections/*/section.xml"):
        try:
            root = ET.parse(f).getroot()
            s = root.find("summary")
            if s is not None and len(list(s)) != 0:
                print(f"  RESUMEN CON HIJOS (HTML sin escapar): {f}")
                bad_summary += 1
                ok = False
        except Exception:
            pass
    print(f"[validate] Secciones con resumen mal escapado: {bad_summary}")

    bad_intro = 0
    for f in glob.glob(f"{vdir}/activities/label_*/label.xml") + glob.glob(f"{vdir}/activities/assign_*/assign.xml"):
        try:
            root = ET.parse(f).getroot()
            container = root.find("label")
            if container is None:
                container = root.find("assign")
            intro = container.find("intro") if container is not None else None
            if intro is not None and len(list(intro)) != 0:
                print(f"  ACTIVITAT CON HIJOS (HTML sin escapar): {f}")
                bad_intro += 1
                ok = False
        except Exception:
            pass
    print(f"[validate] Etiquetes/Tasques amb intro mal escapado: {bad_intro}")

    hidden_labels = 0
    total_labels = 0
    for f in glob.glob(f"{vdir}/activities/label_*/module.xml"):
        total_labels += 1
        try:
            root = ET.parse(f).getroot()
            vis = root.find("visible")
            if vis is not None and vis.text == "0":
                hidden_labels += 1
        except Exception:
            pass
    print(f"[validate] Etiquetes ocultes a l'alumnat: {hidden_labels}/{total_labels}")
    if total_labels and hidden_labels != total_labels:
        ok = False

    assign_names = []
    for f in glob.glob(f"{vdir}/activities/assign_*/assign.xml"):
        root = ET.parse(f).getroot()
        name_el = root.find("assign").find("name")
        assign_names.append(name_el.text if name_el is not None else "?")
    print(f"[validate] Tasques (Practica) generades: {len(assign_names)}")
    dupes = len(assign_names) - len(set(assign_names))
    print(f"[validate] Noms de tasca duplicats: {dupes}")
    if dupes:
        ok = False

    gb = os.path.join(vdir, "gradebook.xml")
    by_id = set()
    crit_ids = set()
    manual_count = 0
    if os.path.exists(gb):
        root = ET.parse(gb).getroot()
        cats = list(root.find("grade_categories"))
        by_id = {c.get("id") for c in cats}
        # "fulla" = categoria sense cap subcategoria filla (independent de la
        # seua profunditat, ja que en el mode nou Criteri/Recursos/Examen no
        # estan tots al mateix nivell). Nomes les fulles poden rebre
        # grade_items reals (mod) o manuals.
        parent_ids = {c.find("parent").text for c in cats}
        crit_ids = by_id - parent_ids
        orphans = 0
        for c in cats:
            p = c.find("parent").text
            if p != "$@NULL@$" and p not in by_id:
                print(f"  CATEGORIA HUERFANA: {c.get('id')} (padre {p} no existe)")
                orphans += 1
                ok = False
        print(f"[validate] Categorias calificador: {len(cats)}  huerfanas: {orphans}")

        gitems = list(root.find("grade_items"))
        manual_orphans = 0
        for gi in gitems:
            if gi.find("itemtype").text == "manual":
                manual_count += 1
                catid = gi.find("categoryid").text
                if catid not in crit_ids:
                    print(f"  ELEMENT MANUAL AMB CATEGORIA INVALIDA: {gi.get('id')} -> {catid}")
                    manual_orphans += 1
                    ok = False
        print(f"[validate] Elements de qualificacio manuals (2n criteri): {manual_count}  amb categoria invalida: {manual_orphans}")

    total_assigns = len(assign_names)
    orphan_mod_items = 0
    not_leaf = 0
    for f in glob.glob(f"{vdir}/activities/assign_*/grades.xml"):
        root = ET.parse(f).getroot()
        for gi in root.iter("grade_item"):
            catid = gi.find("categoryid").text
            if catid != "$@NULL@$" and catid not in by_id:
                print(f"  GRADE_ITEM DE TASCA CON CATEGORIA INEXISTENTE: {f} -> {catid}")
                orphan_mod_items += 1
                ok = False
            elif catid != "$@NULL@$" and catid not in crit_ids:
                print(f"  GRADE_ITEM DE TASCA NO APUNTA A UNA CATEGORIA FULLA: {f} -> {catid}")
                not_leaf += 1
    print(f"[validate] Grade_items de Tasques amb categoria valida: {total_assigns - orphan_mod_items}/{total_assigns}")
    print(f"[validate] Grade_items de Tasques que apunten a un CRITERI concret: {total_assigns - not_leaf}/{total_assigns}")

    # ---- Recursos H5P/SCORM (mode nou): grade_items, referencies de
    # fitxer (inforef <-> files.xml) i blobs realment presents al disc ----
    recurso_dirs = glob.glob(f"{vdir}/activities/h5pactivity_*") + glob.glob(f"{vdir}/activities/scorm_*")
    if recurso_dirs:
        by_id_files = set()
        ffile = os.path.join(vdir, "files.xml")
        if os.path.exists(ffile):
            by_id_files = {f.get("id") for f in ET.parse(ffile).getroot().findall("file")}
        recurso_orphans = 0
        recurso_leaf_bad = 0
        fileref_bad = 0
        blob_missing = 0
        for d in recurso_dirs:
            g = ET.parse(f"{d}/grades.xml").getroot()
            gi = g.find(".//grade_item")
            catid = gi.find("categoryid").text
            if catid != "$@NULL@$" and catid not in by_id:
                print(f"  RECURS AMB CATEGORIA INEXISTENT: {d} -> {catid}")
                recurso_orphans += 1
                ok = False
            elif catid != "$@NULL@$" and catid not in crit_ids:
                print(f"  RECURS NO APUNTA A UNA CATEGORIA FULLA (Recursos): {d} -> {catid}")
                recurso_leaf_bad += 1
                ok = False
            inf = ET.parse(f"{d}/inforef.xml").getroot()
            for fid_el in inf.findall(".//fileref/file/id"):
                if fid_el.text not in by_id_files:
                    print(f"  RECURS AMB FILEREF SENSE ENTRADA A files.xml: {d} -> {fid_el.text}")
                    fileref_bad += 1
                    ok = False
        if os.path.exists(ffile):
            for fr in ET.parse(ffile).getroot().findall("file"):
                sha1 = fr.find("contenthash").text
                size = fr.find("filesize").text
                if size != "0":
                    blob_path = os.path.join(vdir, "files", sha1[:2], sha1)
                    if not os.path.exists(blob_path):
                        print(f"  BLOB DE FITXER NO TROBAT AL DISC: {sha1}")
                        blob_missing += 1
                        ok = False
        print(f"[validate] Recursos H5P/SCORM generats: {len(recurso_dirs)}  amb categoria invalida: {recurso_orphans + recurso_leaf_bad}  amb fileref trencat: {fileref_bad}  amb blob absent: {blob_missing}")

    rubric_files = glob.glob(f"{vdir}/activities/assign_*/grading.xml")
    with_rubric = 0
    rubric_bad = 0
    for f in rubric_files:
        root = ET.parse(f).getroot()
        areas = list(root.findall("area"))
        if not areas:
            continue
        with_rubric += 1
        area = areas[0]
        if area.find("activemethod").text != "rubric":
            print(f"  RUBRICA SENSE activemethod=rubric: {f}")
            rubric_bad += 1
            ok = False
            continue
        criteria = area.findall(".//criterion")
        if not criteria:
            print(f"  RUBRICA SENSE CAP CRITERI: {f}")
            rubric_bad += 1
            ok = False
        for crit in criteria:
            levels = crit.findall(".//level")
            scores = sorted(float(l.find("score").text) for l in levels)
            if len(levels) != 5 or scores != [0.0, 1.0, 2.0, 3.0, 4.0]:
                print(f"  RUBRICA AMB NIVELLS INCORRECTES (esperats 0-4): {f} -> {scores}")
                rubric_bad += 1
                ok = False
    print(f"[validate] Tasques amb rubrica generada: {with_rubric}/{total_assigns}  amb problemes: {rubric_bad}")

    shutil.rmtree(vdir, ignore_errors=True)
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 build_mbz.py curso_data.json [salida.mbz]")
        sys.exit(1)
    data_path = sys.argv[1]
    data = load_data(data_path)
    out_path = sys.argv[2] if len(sys.argv) > 2 else data.get("filename", "curso.mbz")
    tmp_dir, n_sections, n_categories = build(data, out_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"OK -> {out_path}")
    print(f"Secciones: {n_sections}  Categorias calificador: {n_categories}")
    is_valid = validate(out_path)
    if not is_valid:
        print("¡ATENCION! La validacion encontro problemas — revisa el JSON de entrada.")
        sys.exit(2)
    print("Validacion: TODO CORRECTO.")
