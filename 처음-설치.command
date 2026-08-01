#!/bin/bash
# 처음 한 번만 더블클릭하면 됩니다. 필요한 것들을 자동으로 설치해요.
cd "$(dirname "$0")"

echo "📦 처음 설치를 시작합니다. 몇 분 걸릴 수 있어요..."

if ! command -v python3 &> /dev/null; then
  echo "❗ 파이썬이 없어요. 화면에 설치 창이 뜨면 '설치'를 눌러주세요."
  echo "   설치가 끝나면 이 파일을 다시 더블클릭해주세요."
  xcode-select --install 2>/dev/null
  read -p "엔터를 누르면 창이 닫힙니다."
  exit 1
fi

python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "🎞  영상 합치기 도구(ffmpeg)를 준비하는 중..."
python -c "from static_ffmpeg import run; run.get_or_fetch_platform_executables_else_raise()"

echo ""
echo "🔍 설치가 잘 됐는지 점검하는 중..."
if python -c "import flask, yt_dlp, curl_cffi, static_ffmpeg, faster_whisper" 2>/dev/null; then
  echo "✅ 설치 완료! 이제 '실행.command'를 더블클릭하면 됩니다."
else
  echo "❌ 설치가 완전히 끝나지 않았어요."
  echo "   와이파이(인터넷) 연결을 확인한 다음, 이 파일을 다시 더블클릭해주세요."
  echo "   그래도 안 되면 이 창을 사진 찍어서 보내주세요."
fi
read -p "엔터를 누르면 창이 닫힙니다."
