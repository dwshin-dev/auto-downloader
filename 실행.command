#!/bin/bash
# 더블클릭하면 다운로더가 실행되고 브라우저가 자동으로 열립니다
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "❗ 아직 설치가 안 됐어요. 먼저 '처음-설치.command'를 더블클릭해주세요."
  read -p "엔터를 누르면 창이 닫힙니다."
  exit 1
fi

source venv/bin/activate

# 최신 상태로 자동 업데이트 (틱톡 등이 자주 바뀌어서 필요해요. 몇 초면 끝남)
echo "🔄 최신 버전 확인 중..."
git pull --quiet 2>/dev/null
pip install --quiet -r requirements.txt 2>/dev/null
pip install --quiet --upgrade yt-dlp curl-cffi 2>/dev/null

# 아이콘 앱이 없거나 소스가 더 새것이면 다시 만든다
if [ ! -d "영상 다운로더.app" ] || [ tools/앱소스.applescript -nt "영상 다운로더.app" ]; then
  echo "🎨 실행 아이콘을 만드는 중..."
  bash tools/앱만들기.sh > /dev/null 2>&1
fi

# 2초 후 기본 브라우저로 화면 열기
(sleep 2 && open "http://127.0.0.1:5002") &

echo "🎬 영상 소스 다운로더를 시작합니다. 이 창은 닫지 마세요!"
echo "   (끝내려면 이 창을 닫으면 됩니다)"
python app.py
