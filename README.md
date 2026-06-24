# PDF → JSON Extractor (Gratis)

Extrae texto, bloques y tablas de PDFs usando **PyMuPDF** — sin API keys, sin costo.

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Info de la API |
| POST | `/extract` | Sube un PDF, retorna JSON |
| GET | `/health` | Health check |

## Deploy en Render (gratis)

```bash
# 1. Sube a GitHub
git init && git add . && git commit -m "init"
git remote add origin https://github.com/TU_USUARIO/pdf-json-free.git
git push -u origin main

# 2. En render.com → New → Web Service → conecta el repo
#    No necesitas variables de entorno
#    Click "Create Web Service"
```

## Prueba local

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# http://localhost:8000
```

## Prueba con curl

```bash
curl -X POST https://TU-APP.onrender.com/extract \
  -F "file=@documento.pdf"
```
