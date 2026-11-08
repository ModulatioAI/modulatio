# Third-party software in Modulatio

Modulatio's own code is under the Apache License 2.0 (LICENSE; credits in NOTICE). The release
package for Debian and Ubuntu bundles the Python packages below, each unmodified and each keeping
its own licence files in its installed `.dist-info` folder, and a Python interpreter from
python-build-standalone (astral-sh), CPython under the PSF License 2.0 with the libraries it
carries under their own licences. Written by scripts/third_party.py from
packaging/requirements-web.lock.

| Package | Version | Licence |
|---|---|---|
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License; PSF-2.0 |
| aiohttp | 3.14.1 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache Software License; Apache 2.0 |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT License |
| anyio | 4.14.2 | MIT |
| attrs | 26.1.0 | MIT |
| brotli | 1.2.0 | MIT |
| certifi | 2026.6.17 | Mozilla Public License 2.0 (MPL 2.0); MPL-2.0 |
| charset-normalizer | 3.4.9 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| ddgs | 9.14.4 | MIT |
| deprecation | 2.1.0 | Apache Software License; Apache 2 |
| distro | 1.9.0 | Apache Software License; Apache License, Version 2.0 |
| fake-useragent | 2.2.0 | Apache-2.0 |
| fastapi | 0.139.2 | MIT |
| fastembed | 0.8.0 | Other/Proprietary License; Apache License (its licence file: Apache License) |
| fastuuid | 0.14.0 | BSD License |
| filelock | 3.31.0 | MIT |
| flatbuffers | 25.12.19 | Apache Software License; Apache 2.0 |
| frozenlist | 1.8.0 | Apache-2.0 |
| fsspec | 2026.6.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT License; MIT |
| h2 | 4.3.0 | MIT License; The MIT License (MIT) |
| hf-xet | 1.5.2 | Apache-2.0 |
| hpack | 4.2.0 | MIT License; The MIT License (MIT) |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD License; BSD-3-Clause |
| huggingface-hub | 1.24.0 | Apache Software License; Apache-2.0 |
| hyperframe | 6.1.0 | MIT License; The MIT License (MIT) |
| idna | 3.18 | BSD-3-Clause |
| importlib-metadata | 8.9.0 | Apache-2.0 |
| jinja2 | 3.1.6 | BSD License |
| jiter | 0.16.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| lance-namespace | 0.9.0 | Apache-2.0 |
| lance-namespace-urllib3-client | 0.9.0 | Apache-2.0 |
| lancedb | 0.34.0 | Apache Software License |
| linkify-it-py | 2.1.0 | MIT License; MIT |
| litellm | 1.92.0 | MIT |
| loguru | 0.7.3 | MIT License |
| lxml | 6.1.1 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT License |
| markupsafe | 3.0.3 | BSD-3-Clause |
| mdit-py-plugins | 0.6.1 | MIT License |
| mdurl | 0.1.2 | MIT License |
| mmh3 | 5.2.1 | MIT License |
| multidict | 6.7.1 | Apache License 2.0 |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| onnxruntime | 1.27.0 | MIT License |
| openai | 2.46.0 | Apache Software License; Apache-2.0 |
| orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| platformdirs | 4.10.1 | MIT |
| primp | 1.3.1 | MIT License |
| propcache | 0.5.2 | Apache Software License; Apache-2.0 |
| protobuf | 7.35.1 | 3-Clause BSD License |
| py-rust-stemmers | 0.1.8 | not stated in its metadata (its licence file: MIT License) |
| pyarrow | 25.0.0 | Apache-2.0 |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| pygments | 2.20.0 | BSD-2-Clause |
| pyperclip | 1.11.0 | BSD License; BSD |
| python-dateutil | 2.9.0.post0 | BSD License; Apache Software License; Dual License |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| pyyaml | 6.0.3 | MIT License; MIT |
| referencing | 0.37.0 | MIT |
| regex | 2026.7.10 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | Apache Software License; Apache-2.0 |
| rich | 15.0.0 | MIT License; MIT |
| rpds-py | 2026.6.3 | MIT |
| shellingham | 1.5.4 | ISC License (ISCL); ISC License |
| six | 1.17.0 | MIT License; MIT |
| sniffio | 1.3.1 | MIT License; Apache Software License; MIT OR Apache-2.0 |
| socksio | 1.0.0 | MIT License; UNKNOWN (its licence file: MIT License) |
| starlette | 1.3.1 | BSD-3-Clause |
| textual | 8.2.8 | MIT License; MIT |
| tiktoken | 0.13.0 | MIT License |
| tokenizers | 0.23.1 | Apache Software License |
| tqdm | 4.69.0 | MPL-2.0 AND MIT |
| typer | 0.27.0 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| uc-micro-py | 2.0.0 | MIT License; MIT |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.51.0 | BSD-3-Clause |
| yarl | 1.24.2 | Apache-2.0 |
| zipp | 4.1.0 | MIT |

## NOTICE texts of bundled packages

Reproduced as Apache-2.0 section 4(d) asks of a work that redistributes them.

### fastembed 0.8.0

```
Copyright 2024 Qdrant

This product includes software developed by Qdrant

This distribution includes the following Jina AI models, each with its respective license:
- jinaai/jina-colbert-v2
    - License: cc-by-nc-4.0
- jinaai/jina-reranker-v2-base-multilingual
    - License: cc-by-nc-4.0
- jinaai/jina-embeddings-v3
    - License: cc-by-nc-4.0

These models are developed by Jina (https://jina.ai/) and are subject to Jina AI's licensing terms.

This distribution includes the following Google models, each with its respective license:
- vidore/colpali-v1.3
    - License: gemma

Gemma is provided under and subject to the Gemma Terms of Use found at ai.google.dev/gemma/terms

Additional Notes:
This project also includes third-party libraries with their respective licenses. Please refer to the documentation of each library for details regarding its usage and licensing terms.
```

### propcache 0.5.2

```
Copyright 2016-2021, Andrew Svetlov and aio-libs team

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```

### pyarrow 25.0.0

```
Apache Arrow
Copyright 2016-2026 The Apache Software Foundation

This product includes software developed at
The Apache Software Foundation (http://www.apache.org/).

This product includes software from the SFrame project (BSD, 3-clause).
* Copyright (C) 2015 Dato, Inc.
* Copyright (c) 2009 Carnegie Mellon University.

This product includes software from the Feather project (Apache 2.0)
https://github.com/wesm/feather

This product includes software from the DyND project (BSD 2-clause)
https://github.com/libdynd

This product includes software from the LLVM project
 * distributed under the University of Illinois Open Source

This product includes software from the mman-win32 project
 * Copyright https://code.google.com/p/mman-win32/
 * Licensed under the MIT License;

This product includes software from the LevelDB project
 * Copyright (c) 2011 The LevelDB Authors. All rights reserved.
 * Use of this source code is governed by a BSD-style license that can be
 * Moved from Kudu http://github.com/cloudera/kudu

This product includes software from the CMake project
 * Copyright 2001-2009 Kitware, Inc.
 * Copyright 2012-2014 Continuum Analytics, Inc.
 * All rights reserved.

This product includes software from https://github.com/matthew-brett/multibuild (BSD 2-clause)
 * Copyright (c) 2013-2016, Matt Terry and Matthew Brett; all rights reserved.

This product includes software from the Ibis project (Apache 2.0)
 * Copyright (c) 2015 Cloudera, Inc.
 * https://github.com/cloudera/ibis

This product includes software from Dremio (Apache 2.0)
  * Copyright (C) 2017-2018 Dremio Corporation
  * https://github.com/dremio/dremio-oss

This product includes software from Google Guava (Apache 2.0)
  * Copyright (C) 2007 The Guava Authors
  * https://github.com/google/guava

This product include software from CMake (BSD 3-Clause)
  * CMake - Cross Platform Makefile Generator
  * Copyright 2000-2019 Kitware, Inc. and Contributors

The web site includes files generated by Jekyll.

--------------------------------------------------------------------------------

This product includes code from Apache Kudu, which includes the following in
its NOTICE file:

  Apache Kudu
  Copyright 2016 The Apache Software Foundation

  This product includes software developed at
  The Apache Software Foundation (http://www.apache.org/).

  Portions of this software were developed at
  Cloudera, Inc (http://www.cloudera.com/).

--------------------------------------------------------------------------------

This product includes code from Apache ORC, which includes the following in
its NOTICE file:

  Apache ORC
  Copyright 2013-2019 The Apache Software Foundation

  This product includes software developed by The Apache Software
  Foundation (http://www.apache.org/).

  This product includes software developed by Hewlett-Packard:
  (c) Copyright [2014-2015] Hewlett-Packard Development Company, L.P
```

### requests 2.34.2

```
Requests
Copyright 2019 Kenneth Reitz
```

### yarl 1.24.2

```
Copyright 2016-2021, Andrew Svetlov and aio-libs team

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```
