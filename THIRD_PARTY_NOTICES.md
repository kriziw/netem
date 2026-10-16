# Third-party notices

NetEm WAN Lab contains code originally developed for
[Techkarma NetEm](https://github.com/techkarma-no/techkarma-netem) by Techkarma / techkarma-no,
including the initial Flask interface, transparent bridge handling and Linux
`tc/netem` integration.

Original copyright: Copyright (c) 2025 Techkarma.

That code is provided under the MIT License. The original copyright, permission
notice and disclaimer are retained in [LICENSE](LICENSE) and must accompany copies
or substantial portions of that software.

NetEm WAN Lab's ongoing development, releases and support are maintained in
[kriziw/netem](https://github.com/kriziw/netem).


## Authentication dependencies

OIDC support uses [Authlib](https://github.com/authlib/authlib) (BSD-3-Clause)
and [Requests](https://github.com/psf/requests) (Apache-2.0), installed with their
own dependency and license metadata through `requirements.txt`. Local password
hashing uses Werkzeug, distributed with Flask. These packages are dependencies;
their source is not vendored into this repository.
