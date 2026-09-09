FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tesseract-ocr-{guj,nep} are the Gujarati and Nepali trained-data packages
# (Devanagari script for Nepali). They're baked into the image here, never
# fetched at runtime - the /ocr endpoint's `lang` param can only select a
# language whose tessdata is already present. `fonts-lohit-gujr` and
# `fonts-lohit-deva` are NOT used by the service at runtime (it only OCRs
# uploaded images); they're here so the test suite and the eval harness can
# render real Gujarati/Devanagari glyphs into synthetic sample images inside
# this same image. Verify what actually landed with:
#     docker run --rm <image> tesseract --list-langs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-guj \
    tesseract-ocr-nep \
    fonts-lohit-gujr \
    fonts-lohit-deva \
    poppler-utils \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --list-langs

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
