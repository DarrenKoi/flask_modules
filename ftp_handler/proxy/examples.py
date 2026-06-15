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
from pathlib import Path, PurePosixPath

from ftp_handler.proxy import (
    FtpFleetDownloader,
    HostSpec,
    ListDir,
    UploadFile,
    UploadSpec,
    save_to_dir,
    specs_from_hosts,
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


def _local_path(base: Path, host: str, remote_path: str) -> Path:
    """원격 FTP 경로를 base/<host>/... 아래로 미러링한다(하위 폴더 구조 보존)."""
    parts = [p for p in PurePosixPath(remote_path).parts if p not in ("/", "")]
    return base.joinpath(host, *parts)


def example_download_images_with_cond(
    host: str = "10.0.0.1",
    parent: str = "/IMAGES",
    dest: str | Path = r"C:\eqp_downloads",
) -> tuple[list[Path], list[Path | None]]:
    """이미지(``*01AP.jpeg``)와 그 사이드카 cond.txt를 짝지어 받아 로컬에 저장한다.

    고정 규칙: 각 이미지에는 "." + 이미지 파일명으로 된 하위 폴더가 있고 그 안에
    cond.txt가 있다(예: ``S09_M0047-01AP.jpeg`` → ``.S09_M0047-01AP.jpeg/cond.txt``).
    먼저 ``list_dirs``로 이미지 이름만 탐색하고(가져오지 않음), 각 이미지에 대해
    cond.txt 경로를 규칙으로 만들어 둘을 함께 RETR한다. ``on_file``은 파일을 쓰는
    동시에 그 로컬 경로를 (host, remote_path) 키로 기록하므로, 스레드 완료 순서와
    무관하게 결과를 조회할 수 있다.

    반환값은 인덱스가 정렬된 ``(image_paths, cond_paths)``다 — ``cond_paths[i]``는
    ``image_paths[i]``의 cond.txt이며, 서버에 cond.txt가 없으면 ``None``이다. 두
    리스트 모두 정렬된 탐색 순서를 따르므로 cond.txt 하나가 빠져도 정렬이 어긋나지
    않는다. (서버에 없는 파일은 RETR이 550으로 실패해 ``on_file``이 호출되지 않으므로
    반환 리스트에 들어가지 않는다.)
    """
    base = Path(dest)
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)   # 프록시 위치는 PROXY_URL 상수

    def cond_for(image_path: str) -> str:
        p = PurePosixPath(image_path)
        return str(p.with_name(f".{p.name}") / "cond.txt")

    # 1. 이미지 탐색(이름만, 가져오지 않음). 재현 가능한 순서를 위해 정렬한다.
    listing = dl.list_dirs(
        specs_from_hosts([host], listings=[ListDir(parent, "*01AP.jpeg")])
    )
    discovered = sorted((l.host, img) for l in listing.listings for img in l.paths)

    # 2. 호스트당 한 spec: 이미지 + 그 cond.txt를 고정 경로로 묶는다.
    by_host: dict[str, list[str]] = {}
    for h, img in discovered:
        by_host.setdefault(h, []).extend((img, cond_for(img)))
    specs = [HostSpec(host=h, files=files) for h, files in by_host.items()]

    # 3. 파일을 쓰면서 로컬 경로를 (host, remote_path) 키로 기록한다(순서 무관).
    written: dict[tuple[str, str], Path] = {}

    def on_file(h: str, remote_path: str, data: bytes) -> None:
        target = _local_path(base, h, remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written[(h, remote_path)] = target   # list.append이 아니라 키 기록 → 스레드 안전

    dl.download(specs, on_file=on_file)

    # 4. 콜백 순서가 아니라 탐색 순서를 따라 인덱스가 정렬된 리스트를 만든다.
    image_paths: list[Path] = []
    cond_paths: list[Path | None] = []
    for h, img in discovered:
        img_local = written.get((h, img))
        if img_local is None:
            continue   # 이미지 자체가 없음 — 정렬 유지를 위해 짝 전체를 건너뛴다
        image_paths.append(img_local)
        cond_paths.append(written.get((h, cond_for(img))))   # cond 없으면 None
    return image_paths, cond_paths


def get_date_key_for_sorting(folder_name: str) -> str:
    """정렬 키 — 당신의 기존 함수로 교체하라(여기서는 자리표시자).

    날짜 기반 폴더명(예: ``"20260615"``)을 받아 정렬용 키를 돌려준다. 아래
    ``example_download_latest_images_with_cond``가 이 키로 폴더를 내림차순 정렬해
    가장 최신 폴더를 고른다.
    """
    return folder_name


def example_download_latest_images_with_cond(
    host: str = "10.0.0.1",
    root: str = "/IMAGES",
    dest: str | Path = r"C:\eqp_downloads",
) -> tuple[list[Path], list[Path | None]]:
    """``root`` 아래 최신 날짜 폴더를 고른 뒤 그 안의 이미지+cond.txt를 받는다.

    이미지 폴더로 들어가기 전에, 날짜 기반 폴더들을 먼저 나열하고 기존 정렬 로직
    ``sorted(folders, key=get_date_key_for_sorting, reverse=True)``으로 최신 폴더를
    고른다. 그 폴더를 ``parent``로 삼아 ``example_download_images_with_cond``에
    위임하므로, 이미지(``*01AP.jpeg``)와 사이드카 cond.txt 처리는 그대로 재사용된다.

    반환값은 ``example_download_images_with_cond``와 동일한 인덱스 정렬
    ``(image_paths, cond_paths)``다. ``root``에 폴더가 하나도 없으면 빈 리스트 둘을
    돌려준다.
    """
    dl = FtpFleetDownloader(user=USER, password=PASSWORD)   # 프록시 위치는 PROXY_URL 상수

    # 1. root 아래 항목을 나열한다(이름만, 가져오지 않음). 날짜 폴더만 있다고 가정.
    listing = dl.list_dirs(specs_from_hosts([host], listings=[ListDir(root, None)]))
    folders = [p for l in listing.listings for p in l.paths]
    if not folders:
        return [], []

    # 2. 기존 정렬 키로 내림차순 정렬해 최신 폴더를 고른다(폴더명 기준).
    latest = sorted(
        folders,
        key=lambda p: get_date_key_for_sorting(PurePosixPath(p).name),
        reverse=True,
    )[0]

    # 3. 최신 폴더로 들어가 이미지+cond.txt를 받는다(위 함수 재사용).
    return example_download_images_with_cond(host, latest, dest)


if __name__ == "__main__":
    # example_run_the_proxy_server()      # 프록시 호스트에서
    # example_download_through_proxy()    # 방화벽 안 클라이언트에서
    # images, conds = example_download_images_with_cond()
    # images, conds = example_download_latest_images_with_cond()
    pass
