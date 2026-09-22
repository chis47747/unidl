"""Tests for Movistar Plus+ LG TV (PlayReady / SmoothStreaming) service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from unidl.core.flow import FlowContext
from unidl.core.playback import DrmInfo
from unidl.core.service import AuthStatus, ServiceContext
from unidl.services.movistar import Movistar, api

SAMPLE_DIRECTORY = {
    "services": {
        "Context": {"token": "false"},
        "host": [
            {"@id": "cache", "@address": "http://ottcache.dof6.com"},
            {"@id": "default", "@address": "http://homeservice.dof6.com"},
        ],
        "service": [
            {
                "@name": "prisa/prisatv/vod/autenticacion",
                "endpoint": [
                    {
                        "@name": "autenticacion",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/autenticacion",
                    }
                ],
            },
            {
                "@name": "prisa/prisatv/vod/devices",
                "endpoint": [
                    {
                        "@name": "consulta_idPR",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{ORIGIN}/device/{DEVICEID}/account/{ACCOUNTNUMBER}/mediaplayerid",
                    },
                    {
                        "@name": "activacion_dispositivo_cuenta",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{ORIGIN}/device/{DEVICEID}/account/{ACCOUNTNUMBER}/mediaplayer/{MEDIAPLAYERID}/activate",
                    },
                    {
                        "@name": "setUpStream",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{MEDIAPLAYERID}/profile/{PID}/stream",
                    },
                    {
                        "@name": "tearDownStream",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{MEDIAPLAYERID}/profile/{PID}/stream/{SessionID}",
                    },
                ],
            },
            {
                "@name": "prisa/prisatv/vod/initdata",
                "endpoint": [
                    {
                        "@name": "initdata",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/initdata/mediaplayer/{MEDIAPLAYERID}",
                    }
                ],
            },
            {
                "@name": "prisa/prisatv/vod/license",
                "endpoint": [
                    {
                        "@name": "url_video",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/license/registration.isml/Manifest",
                    },
                    {
                        "@name": "server",
                        "@address": "http://licensing.dof6.com/license/{ORIGIN}/{ACCOUNTNUMBER}/{DUID}",
                    },
                ],
            },
            {
                "@name": "prisa/prisatv/vod/TV",
                "endpoint": [
                    {
                        "@name": "canales",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/TV/{PROFILE}/canales",
                    }
                ],
            },
            {
                "@name": "prisa/prisatv/vod/consultas",
                "endpoint": [
                    {
                        "@name": "buscar",
                        "@address": "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/consultas/{PROFILE}/buscar/{texto}/{NMI}/{true/false}/{start}/{end}",
                    }
                ],
            },
        ],
    }
}


def make_mock_response(
    status_code: int = 200,
    json_data: Any = None,
    content: bytes = b"",
    text: str = "",
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    if json_data is not None:
        raw = json.dumps(json_data).encode("utf-8")
        resp._content = raw
        resp.headers["Content-Type"] = "application/json"
    elif content:
        resp._content = content
    elif text:
        resp._content = text.encode("utf-8")
    else:
        resp._content = b""
    return resp


@pytest.fixture
def mock_session():
    session = MagicMock(spec=requests.Session)
    return session


@pytest.fixture
def mock_api(mock_session):
    client = api.MovistarApi(
        session=mock_session,
        quality="auto",
        username="test@example.com",
        password="secretpassword",
    )
    # Pre-populate directory endpoints
    client.directory._endpoints = {
        ("prisa/prisatv/vod/autenticacion", "autenticacion"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/autenticacion",
        ("prisa/prisatv/vod/devices", "consulta_idPR"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{ORIGIN}/device/{DEVICEID}/account/{ACCOUNTNUMBER}/mediaplayerid",
        ("prisa/prisatv/vod/devices", "activacion_dispositivo_cuenta"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{ORIGIN}/device/{DEVICEID}/account/{ACCOUNTNUMBER}/mediaplayer/{MEDIAPLAYERID}/activate",
        ("prisa/prisatv/vod/devices", "setUpStream"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{MEDIAPLAYERID}/profile/{PID}/stream",
        ("prisa/prisatv/vod/devices", "tearDownStream"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/devices/{MEDIAPLAYERID}/profile/{PID}/stream/{SessionID}",
        ("prisa/prisatv/vod/initdata", "initdata"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/initdata/mediaplayer/{MEDIAPLAYERID}",
        ("prisa/prisatv/vod/license", "url_video"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/license/registration.isml/Manifest",
        ("prisa/prisatv/vod/license", "server"): "http://licensing.dof6.com/license/{ORIGIN}/{ACCOUNTNUMBER}/{DUID}",
        ("prisa/prisatv/vod/TV", "canales"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/TV/{PROFILE}/canales",
        ("prisa/prisatv/vod/consultas", "buscar"): "http://homeservice.dof6.com/VoD/vod.svc/prisa/prisatv/vod/consultas/{PROFILE}/buscar/{texto}/{NMI}/{true/false}/{start}/{end}",
    }
    client.directory.hosts = {"cache": "http://ottcache.dof6.com", "default": "http://homeservice.dof6.com"}
    return client


# -----------------------------------------------------------------------------
# 1. Metadata and Registration Tests
# -----------------------------------------------------------------------------
def test_movistar_service_metadata():
    assert Movistar.ID == "movistar"
    assert Movistar.NAME == "Movistar Plus+"
    assert Movistar.DRM_SYSTEMS == ("playready",)
    assert Movistar.SUPPORTS_LIVE is True
    assert Movistar.SUPPORTS_SEARCH is True
    assert Movistar.SUPPORTS_URL is True
    assert len(Movistar.SETTINGS) == 1
    assert Movistar.SETTINGS[0].key == "source_quality"


def test_endpoint_directory_prepare(mock_api):
    url = "http://licensing.dof6.com/license/{ORIGIN}/{ACCOUNTNUMBER}/{DUID}"
    prepared = mock_api.directory.prepare(
        url,
        {"ORIGIN": "YOMVI", "ACCOUNTNUMBER": "12345", "DUID": "device-uuid-99"},
    )
    assert prepared == "http://licensing.dof6.com/license/YOMVI/12345/device-uuid-99"


# -----------------------------------------------------------------------------
# 2. Authentication Tests
# -----------------------------------------------------------------------------
def test_lg_authentication_flow(mock_api, mock_session):
    login_response = {
        "pr_usuario_cifrado": {
            "ofertas": [
                {
                    "accountNumber": "ACC12345",
                    "@id_perfil": "PERF99",
                    "@ind_principal": "S",
                    "origen": "YOMVI",
                }
            ]
        }
    }
    init_data_response = {
        "token": "tok_xyz_123",
        "pid": "PID_001",
        "id_perfil": "PERF99",
        "parentalRating": "M18",
    }

    def mock_request(method, url, **kwargs):
        if "prisa/prisatv/vod/autenticacion" in url:
            return make_mock_response(200, json_data=login_response)
        if "consulta_idPR" in url or "mediaplayerid" in url:
            return make_mock_response(200, text="MPID_555")
        if "initdata" in url:
            return make_mock_response(200, json_data=init_data_response)
        return make_mock_response(404)

    mock_session.request.side_effect = mock_request

    state = mock_api.authenticate(force=True)
    assert state.account_number == "ACC12345"
    assert state.lg_media_player_id == "MPID_555"
    assert state.lg_origin == "YOMVI"
    assert state.init_data["token"] == "tok_xyz_123"
    assert mock_api.signed_in is True
    assert mock_api.playback_profile == "PERF99"


def test_auth_status():
    ctx = MagicMock(spec=ServiceContext)
    ctx.settings = {"source_quality": "auto"}
    ctx.credential.return_value = MagicMock(username="user@test.com", password="pass")
    tokens_mock = MagicMock()
    tokens_mock.read.return_value = {
        "drm_system": "playready",
        "account_number": "ACC123",
        "device_id": "DEV123",
        "lg_media_player_id": "MP123",
        "init_data": {"token": "valid_token"},
    }
    ctx.tokens = tokens_mock
    svc = Movistar(ctx)
    status = svc.auth_status()
    assert status.logged_in is True
    assert "us***@test.com" in status.label or "account session" in status.label
    assert "PlayReady" in status.detail


# -----------------------------------------------------------------------------
# 3. VOD Resolution and Playback Tests
# -----------------------------------------------------------------------------
def test_resolve_movie(mock_api, mock_session):
    mock_api.state.account_number = "ACC123"
    mock_api.state.device_id = "DEV123"
    mock_api.state.lg_media_player_id = "MP123"
    mock_api.state.init_data = {"token": "tok", "id_perfil": "P1", "pid": "PID1"}
    mock_api._authenticated = True

    movie_detail = {
        "Id": "1001",
        "Titulo": "Test Movie",
        "TipoContenido": "pelicula",
        "Duracion": 115,
        "VodItems": [
            {
                "AssetType": "VOD",
                "CasId": "CAS_MOVIE_1",
                "VideoUrl": "http://vod.dof6.com/movie/test.isml/Manifest",
                "FormatoVideo": "HD",
            }
        ],
    }

    mock_session.request.return_value = make_mock_response(200, json_data=movie_detail)

    resolved = mock_api.resolve("1001")
    assert resolved.id == "1001"
    assert resolved.name == "Test Movie"
    assert resolved.content is not None
    assert resolved.content.kind == "movie"
    assert resolved.content.duration_minutes == 115


def test_resolve_series_and_episodes(mock_api, mock_session):
    mock_api.state.account_number = "ACC123"
    mock_api.state.device_id = "DEV123"
    mock_api.state.lg_media_player_id = "MP123"
    mock_api.state.init_data = {"token": "tok", "id_perfil": "P1", "pid": "PID1"}
    mock_api._authenticated = True

    series_detail = {
        "Id": "2000",
        "TituloSerie": "Test Series",
        "TipoContenido": "serie",
        "Temporadas": [
            {
                "NumeroTemporada": 1,
                "Titulo": "Season 1",
                "Episodios": [
                    {
                        "Id": "2001",
                        "NumeroEpisodio": 1,
                        "TituloEpisodio": "Pilot",
                        "Duracion": 50,
                        "VodItems": [
                            {
                                "AssetType": "VOD",
                                "CasId": "CAS_EP_1",
                                "VideoUrl": "http://vod.dof6.com/ep1.isml/Manifest",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    mock_session.request.return_value = make_mock_response(200, json_data=series_detail)

    resolved = mock_api.resolve("2000")
    assert resolved.id == "2000"
    assert resolved.name == "Test Series"
    assert len(resolved.seasons) == 1
    assert resolved.seasons[0].number == 1
    assert len(resolved.seasons[0].episodes) == 1
    ep = resolved.seasons[0].episodes[0]
    assert ep.name == "Pilot"
    assert ep.number == 1


def test_vod_source_and_session_cleanup(mock_api, mock_session):
    mock_api.state.account_number = "ACC123"
    mock_api.state.device_id = "DEV123"
    mock_api.state.lg_media_player_id = "MP123"
    mock_api.state.init_data = {"token": "tok", "id_perfil": "P1", "pid": "PID1"}
    mock_api._authenticated = True

    movie_detail = {
        "Id": "1001",
        "Titulo": "Test Movie",
        "TipoContenido": "pelicula",
        "VodItems": [
            {
                "AssetType": "VOD",
                "CasId": "CAS_MOVIE_1",
                "VideoUrl": "http://vod.dof6.com/movie/test.isml/Manifest",
                "FormatoVideo": "HD",
            }
        ],
    }

    def mock_request(method, url, **kwargs):
        if "contents/1001/details" in url:
            return make_mock_response(200, json_data=movie_detail)
        if method == "POST" and "/stream" in url:
            return make_mock_response(200, json_data={"resultCode": "0", "resultData": {"sessionID": "SESS_VOD_99"}})
        if method == "DELETE" and "/stream" in url:
            return make_mock_response(200)
        return make_mock_response(404)

    mock_session.request.side_effect = mock_request

    content = api.Content(id="1001", name="Test Movie", kind="movie")
    source = mock_api.source(content)

    assert source.system == "playready"
    assert source.manifest == "http://vod.dof6.com/movie/test.isml/Manifest"
    assert "ACC123" in source.custom_data
    assert "CAS_MOVIE_1" in source.custom_data
    assert source.stream_session is not None
    assert source.stream_session.session_id == "SESS_VOD_99"

    # Verify session close triggers tearDownStream
    closed = source.close()
    assert closed is True


# -----------------------------------------------------------------------------
# 4. Live TV Tests
# -----------------------------------------------------------------------------
def test_channels_and_live_source(mock_api, mock_session):
    mock_api.state.account_number = "ACC123"
    mock_api.state.device_id = "DEV123"
    mock_api.state.lg_media_player_id = "MP123"
    mock_api.state.init_data = {"token": "tok", "id_perfil": "P1", "pid": "PID1"}
    mock_api._authenticated = True

    channels_data = [
        {
            "Uid": "101",
            "Nombre": "La 1",
            "CasId": "CAS_LA1",
            "CodCadenaTv": "TVE1",
            "Dial": 1,
            "FormatoVideo": "HD",
            "PuntoReproduccion": "http://live.dof6.com/la1.isml/Manifest",
        },
        {
            "Uid": "446362",
            "Nombre": "ETB 1",
            "CasId": "1000005531",
            "CodCadenaTv": "ETB1",
            "Dial": 2,
            "FormatoVideo": "HD",
            "PuntoReproduccion": "http://etb1onsat-hlsfp-movistarplus.emisiondof6.com/index.m3u8",
        },
    ]

    def mock_request(method, url, **kwargs):
        if "prisa/prisatv/vod/TV/P1/canales" in url:
            return make_mock_response(200, json_data=channels_data)
        if method == "POST" and "/stream" in url:
            return make_mock_response(200, json_data={"resultCode": "0", "resultData": {"sessionID": "SESS_LIVE_01"}})
        return make_mock_response(404)

    mock_session.request.side_effect = mock_request

    channels = mock_api.channels()
    assert len(channels) == 2
    assert channels[0].name == "La 1"
    assert channels[0].dial == 1

    # Test live_source on ETB 1 (should apply LG_MANIFEST_CORRECTIONS)
    etb1 = channels[1]
    source, _now = mock_api.live_source(etb1)
    assert source.system == "playready"
    assert source.manifest == "http://etb1onsat-pry-movistarplus.emisiondof6.com/index.isml/Manifest"
    assert "ACC123" in source.custom_data
    assert "1000005531" in source.custom_data
    assert source.is_live is True


# -----------------------------------------------------------------------------
# 5. Search Tests
# -----------------------------------------------------------------------------
def test_search_and_channel_resolution(mock_api, mock_session):
    mock_api.state.account_number = "ACC123"
    mock_api.state.device_id = "DEV123"
    mock_api.state.lg_media_player_id = "MP123"
    mock_api.state.init_data = {"token": "tok", "id_perfil": "P1", "pid": "PID1", "parentalRating": "M18"}
    mock_api._authenticated = True

    search_data = {
        "Contenidos": [
            {
                "DatosEditoriales": {
                    "Id": "9001",
                    "Titulo": "El Clásico",
                    "TipoContenido": "evento",
                    "Sinopsis": "Real Madrid vs Barcelona",
                },
                "PuntoReproduccion": "http://live.dof6.com/laliga.isml/Manifest",
                "CasId": "CAS_LALIGA",
                "CodCadenaTv": "LALIGA",
                "Uid": "500",
            },
            {
                "DatosEditoriales": {
                    "Id": "9002",
                    "Titulo": "Inception",
                    "TipoContenido": "pelicula",
                    "Sinopsis": "A dream within a dream",
                },
                "VodItems": [{"AssetType": "VOD", "CasId": "CAS_INC"}],
            },
        ]
    }

    mock_session.request.return_value = make_mock_response(200, json_data=search_data)

    hits = mock_api.search("clasico")
    assert len(hits) == 2
    assert hits[0].name == "El Clásico"
    assert hits[0].route == "LIVE"
    assert hits[1].name == "Inception"
    assert hits[1].route == "VOD"

    # Convert live search hit to Channel
    channel = mock_api.search_channel(hits[0])
    assert channel.id == "500"
    assert channel.name == "El Clásico"
    assert channel.manifest == "http://live.dof6.com/laliga.isml/Manifest"


# -----------------------------------------------------------------------------
# 6. PlayReady License Test
# -----------------------------------------------------------------------------
def test_playready_license_acquisition(mock_api, mock_session):
    mock_session.request.return_value = make_mock_response(
        200, text="<soap:Envelope><soap:Body><AcquireLicenseResponse/></soap:Body></soap:Envelope>"
    )

    source = api.PlaybackSource(
        manifest="http://vod.dof6.com/test.isml/Manifest",
        system="playready",
        license_url="http://licensing.dof6.com/license/server",
        license_token="",
        headers={},
        asset_type="VOD",
        client=mock_api,
    )

    response_text = mock_api.playready_license("<Challenge>data</Challenge>", source)
    assert "<AcquireLicenseResponse/>" in response_text
    mock_session.request.assert_called_with(
        "POST",
        "http://licensing.dof6.com/license/server",
        headers={"Content-Type": "text/xml; charset=UTF-8", "User-Agent": api.USER_AGENT},
        data="<Challenge>data</Challenge>",
        timeout=30,
        verify=False,
    )
