FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    fontconfig \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先に全部のファイルをコピーしちゃう（requirements.txtも最新になる）
COPY . .

# その最新のファイルを使ってライブラリをインストール
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /usr/share/fonts/truetype/custom && \
    if [ -d "fonts" ]; then cp fonts/* /usr/share/fonts/truetype/custom/ 2>/dev/null || true; fi && \
    fc-cache -fv

CMD ["python", "bot.py"]
