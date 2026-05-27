"""ftp_handler.proxy 사용 예제 — 방화벽 안 클라이언트를 위한 HTTP 전송 방식.

두 대의 장비에서 절반씩 실행된다:
  - 서버(SERVER): FTP 서버에 접근 가능한 사내 호스트에서 flask_proxy 실행.
  - 클라이언트(CLIENT): 방화벽에 막힌 PC. 프록시에는 닿지만 FTP 서버에는 못
    닿는다. proxy.FtpFleetDownloader를 direct 버전과 똑같이 사용한다.

클라이언트 쪽 인터페이스는 direct_downloader와 완전히 동일하다 — 유일한 차이는
import 줄뿐이라, 호출부는 다른 변경 없이 전송 방식만 바꿀 수 있다.
테스트가 아니라 복사해 붙여 쓰는 참고용 코드다.
"""

# 클라이언트 쪽: direct 다운로더와 같은 이름들을 HTTP 너머로 사용한다.
from ftp_handler.proxy import (
    FtpFleetDownloader,
    HostSpec,
    ListDir,
    UploadFile,
    UploadSpec,
    save_to_dir,
    upload_specs_from_hosts,
)

USER = "ftpuser"
PASSWORD = "ftppass"
FLEET_HOSTS = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def example_run_the_proxy_server() -> None:
    """서버 절반 — FTP 서버에 닿을 수 있는, 방화벽 없는 호스트에서 실행한다.

    기존 Flask 앱에 블루프린트를 붙이거나 단독으로 실행한다. 인증 없음: 신뢰하는
    단일 사용자뿐이라 FTP_PROXY_TOKEN은 설정하지 않으며 모든 요청이 그대로 통과한다.
    다만 포트가 신뢰할 수 없는 네트워크에 노출되지 않게만 하라(FTP 비밀번호와 파일
    바이트가 이 연결을 평문으로 오간다).

        from ftp_handler.proxy.flask_proxy import ftp_proxy_sknn_v3
        app.register_blueprint(ftp_proxy_sknn_v3)
    """
    from ftp_handler.proxy.flask_proxy import create_app

    create_app().run(host="0.0.0.0", port=8080)


def example_download_through_proxy() -> None:
    """클라이언트 절반 — direct 다운로더의 대체재로 HTTP 너머에서 동작한다.

    프록시 위치는 proxy_downloader.py 상단의 모듈 상수 PROXY_URL로 지정한다(생성자
    인자가 아니다 — 그래야 생성자 시그니처가 direct 다운로더와 똑같아서 import 한
    줄만 바꿔도 깨지지 않는다). 토큰 없음: 프록시는 인증 없이 동작한다. on_file은
    여전히 여기 클라이언트에서 실행되므로 save_to_dir는 파일을 로컬 PC에 떨군다.

        # proxy_downloader.py 상단에서 한 번만 편집:
        # PROXY_URL = "http://proxy.host:8080"
    """
    specs = [HostSpec(host, listings=[ListDir("/MEAS", "*.dat")]) for host in FLEET_HOSTS]
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)   # 프록시 위치는 PROXY_URL 상수
    report = dl.download(specs, on_file=save_to_dir(r"C:\eqp_downloads"))
    print(f"ok={report.ok} ng={report.ng}")


def example_upload_through_proxy() -> None:
    """클라이언트 절반 — 메모리상의 바이트를 프록시 너머로 원격 FTP에 올린다.

    ``UploadFile``은 디스크 파일이 아니라 raw ``bytes``를 받는다. 클라이언트가
    바이트를 base64로 실어 보내면 프록시가 풀어서 STOR한다. download의 ``request_batch``
    와 동일한 배치 방식으로 프록시 쪽 메모리를 제한한다(ADR 0001, 요청 방향에 적용).
    download과 마찬가지로 import 한 줄만 바꾸면 direct 버전과 동일하게 쓸 수 있다.
    """
    payload = b"col_a,col_b\n1,2\n"  # 예: df.to_csv().encode(); 디스크를 거치지 않음
    specs = upload_specs_from_hosts(
        FLEET_HOSTS, files=[UploadFile("/INBOX/report.csv", payload)]
    )
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)   # 프록시 위치는 PROXY_URL 상수
    report = dl.upload(specs)
    print(f"ok={report.ok} ng={report.ng}")


def example_swap_direct_for_proxy() -> None:
    """이 분리 구조의 핵심: import 한 줄만 바꾸면 전송 방식이 바뀐다.

        # direct (방화벽 없는 호스트):
        from ftp_handler.direct_downloader import FtpFleetDownloader
        # 프록시 경유 (방화벽 안 클라이언트):
        from ftp_handler.proxy import FtpFleetDownloader

    아래의 모든 것 — specs, download(), report, on_file — 은 완전히 동일하다.
    """
    specs = [HostSpec(host, files=["/HITACHI/SYSFILE/LOG_RECIPE_EXE.log"]) for host in FLEET_HOSTS]
    report = FtpFleetDownloader(user=USER, password=PASSWORD).download(specs)
    print(report.grouped().keys())


if __name__ == "__main__":
    # example_run_the_proxy_server()      # 프록시 호스트에서
    # example_download_through_proxy()    # 방화벽 안 클라이언트에서
    pass
