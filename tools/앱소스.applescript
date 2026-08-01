-- 영상 다운로더 실행 앱
-- 터미널 창 없이 백그라운드로 서버를 켜고, 켜져 있는 동안 독에 남아 있는다.
-- 독 아이콘 우클릭 → 종료 하면 서버도 같이 꺼진다.
-- 이 파일을 고치면 tools/앱만들기.sh 를 다시 돌려야 반영된다.
--
-- ⚠️ property는 쓰지 말 것. stay-open 앱은 종료할 때 property 값을 앱 파일에
--    되쓰려고 하는데, 서명된 앱은 쓰기가 막혀 있어서 종료가 먹통이 된다.
--    상수는 아래처럼 핸들러로 만든다.

on serverURL()
	return "http://127.0.0.1:5002"
end serverURL

on portNum()
	return "5002"
end portNum

on projectDir()
	-- 앱은 프로젝트 폴더 안에 있다. 앱 경로에서 한 단계 올라가면 프로젝트 폴더.
	set appPath to POSIX path of (path to me)
	if appPath ends with "/" then set appPath to text 1 thru -2 of appPath
	set AppleScript's text item delimiters to "/"
	set parts to text items of appPath
	set parts to items 1 thru -2 of parts
	set dirPath to parts as text
	set AppleScript's text item delimiters to ""
	return dirPath
end projectDir

on serverRunning()
	try
		do shell script "/usr/sbin/lsof -ti :" & my portNum()
		return true
	on error
		return false
	end try
end serverRunning

on notify(msg)
	try
		display notification msg with title "영상 소스 다운로더"
	end try
end notify

on run
	set projDir to my projectDir()

	-- 이미 켜져 있으면 화면만 다시 열어준다
	if my serverRunning() then
		open location my serverURL()
		return
	end if

	-- 아직 설치를 안 했으면 안내하고 끝낸다
	try
		do shell script "test -d " & quoted form of (projDir & "/venv")
	on error
		display dialog "아직 설치가 안 됐어요." & return & return & ¬
			"먼저 '처음-설치.command'를 더블클릭해서 설치를 끝내주세요." ¬
			buttons {"알겠어요"} default button 1 with icon caution
		quit
		return
	end try

	my notify("최신 버전 확인 중...")
	try
		-- 최신 코드와 부품으로 맞춘다 (실패해도 그냥 기존 것으로 실행)
		do shell script "cd " & quoted form of projDir & " && " & ¬
			"git pull --quiet; " & ¬
			"./venv/bin/pip install --quiet -r requirements.txt; " & ¬
			"./venv/bin/pip install --quiet --upgrade yt-dlp curl-cffi" & ¬
			" > /dev/null 2>&1"
	end try

	my notify("프로그램을 켜는 중...")
	-- 터미널 없이 백그라운드로 서버 실행.
	-- 내 PID를 넘겨줘서, 이 앱이 사라지면 서버가 알아서 같이 꺼지게 한다.
	--
	-- ⚠️ 반드시 괄호로 서브셸을 한 겹 더 씌워서 완전히 떼어놓아야 한다.
	--    그냥 '&'로만 띄우면 서버가 앱의 입출력을 붙잡고 있어서,
	--    나중에 독에서 종료할 때 앱이 안 꺼진다. (원인 찾는 데 한참 걸림)
	set myPID to do shell script "echo $PPID"
	do shell script "cd " & quoted form of projDir & " && " & ¬
		"( WATCH_APP_PID=" & myPID & " nohup ./venv/bin/python app.py " & ¬
		"> /tmp/영상다운로더.log 2>&1 < /dev/null & ) &"

	-- 서버가 다 뜰 때까지 기다린다 (최대 40초).
	-- 애플스크립트로 여러 번 확인하지 말고 셸에서 한 번에 기다리게 한다.
	set serverUp to true
	try
		do shell script "for i in $(seq 1 80); do " & ¬
			"/usr/sbin/lsof -ti :" & my portNum() & " > /dev/null 2>&1 && exit 0; " & ¬
			"sleep 0.5; done; exit 1"
	on error
		set serverUp to false
	end try

	if not serverUp then
		display dialog "프로그램을 켜지 못했어요." & return & return & ¬
			"'실행.command'를 더블클릭하면 무엇이 잘못됐는지 볼 수 있어요." ¬
			buttons {"알겠어요"} default button 1 with icon stop
		quit
		return
	end if

	open location my serverURL()
end run

-- stay-open 앱이라 실행이 끝나도 독에 아이콘이 남는다 (우클릭 → 종료로 끌 수 있음).
-- idle 안에서는 아무것도 하지 않는다 — 여기서 do shell script를 부르면
-- 종료 이벤트와 부딪힌다. 화면의 '끄기' 버튼을 누르면 서버가 이 앱을 직접 종료시킨다.
on idle
	return 5
end idle

-- on quit 핸들러는 두지 않는다. 직접 만들면 앱이 안 꺼지는 문제가 있었다.
-- 앱이 꺼지면 서버가 (WATCH_APP_PID를 지켜보다가) 알아서 같이 꺼진다.
