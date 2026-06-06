# 1. ベースとしてPython 3.11の軽量公式イメージを使用
FROM python:3.11-slim

# 2. システムの更新と ffmpeg、その他必要なツールのインストール
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. コンテナ内の作業ディレクトリを /app に設定
WORKDIR /app

# 4. requirements.txt をコピーしてライブラリをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 残りのプログラム（bot.pyなど）をすべてコンテナにコピー
COPY . .

# 6. Botを実行するコマンド
CMD ["python", "bot.py"]
