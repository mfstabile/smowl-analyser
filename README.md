# smowl-analyser

CLI em Python para extrair dados do Blackboard + SMOW em arquivos locais auditáveis.

A URL padrão do Blackboard é `https://insper.blackboard.com`. Use `--url` apenas se precisar
apontar para outro ambiente.

## Instalação

### Requisitos

Instale antes de usar:

- Python 3.12, recomendado;
- `pip`;
- Chromium gerenciado pelo Playwright;

O projeto declara estas dependências Python em `pyproject.toml`:

- `playwright`: automação do navegador;
- `typer`: interface de linha de comando;
- `rich`: tabelas e mensagens no terminal;
- `pydantic`: modelos e validação dos JSONs;
- `beautifulsoup4`: leitura de HTML como fallback;
- `certifi`: certificados usados em downloads HTTPS;
- `pytest`, apenas para desenvolvimento/testes.

### Ambiente virtual

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Se o seu comando local for apenas `python3`, use:

```bash
python3 -m venv .venv
```

Depois da instalação, confira se o comando foi registrado:

```bash
smowl-analyser --help
```

## Primeiro Uso

### Verificar ambiente

```bash
smowl-analyser doctor
```

Esse comando verifica se as dependências estão importáveis, se a sessão do Playwright já existe e
onde as execuções serão salvas. Ele não abre Blackboard nem SMOW.

### Fazer login

```bash
smowl-analyser login
```

O comando abre um Chromium visível. Faça login manualmente no Blackboard, incluindo SSO/MFA quando
necessário. Depois volte ao terminal e pressione Enter.

A sessão autenticada fica salva em:

```text
.secrets/playwright-state.json
```

O programa não armazena sua senha. Os próximos comandos reutilizam essa sessão.

Opções úteis:

```bash
smowl-analyser login --slow-mo-ms 250
smowl-analyser login --url "https://outro.blackboard.example"
```

## Fluxo Recomendado

Para baixar a turma inteira:

```bash
smowl-analyser login
smowl-analyser extract
smowl-analyser analyze --run-id "<run_id>"
```

Para baixar somente um aluno:

```bash
smowl-analyser login
smowl-analyser extract-student
```

Use `resume` quando uma extração for interrompida. Use os comandos de diagnóstico do fim deste
README somente quando for necessário investigar falhas ou evoluir o crawler.

## Modos De Extração

### `extract`

Modo principal para extrair uma turma inteira a partir da lista de alunos do SMOW.

```bash
smowl-analyser extract
```

Fluxo:

1. O navegador abre no Blackboard usando a sessão salva.
2. Você navega manualmente até a página do SMOW onde aparece a lista de alunos.
3. Quando a lista estiver carregada, volte ao terminal e pressione Enter.
4. O programa captura as respostas internas do SMOW, identifica os alunos e baixa os dados.

Para cada aluno, o comando:

- busca o JSON de ComputerMonitoring;
- salva `students/{student_id}/computer_monitoring.json`;
- baixa as imagens/evidências vinculadas ao ComputerMonitoring;
- salva os arquivos em `files/{student_id}/...`;
- atualiza `progress.json` aluno por aluno.

Use `--download-workers` para controlar downloads paralelos de imagens:

```bash
smowl-analyser extract --download-workers 8
```

Use `--download-workers 1` para baixar de forma sequencial.

### `extract-student`

Modo para baixar somente um aluno específico.

```bash
smowl-analyser extract-student
```

Fluxo:

1. O navegador abre no Blackboard usando a sessão salva.
2. Você navega manualmente até a página de evidências de
   Computer Monitoring desse aluno na aba Full report.
3. Aguarde o ComputerMonitoring carregar.
4. Volte ao terminal e pressione Enter.

O comando procura o `userId` capturado nas chamadas de ComputerMonitoring. Se mais de um aluno
aparecer nas respostas durante a navegação, ele mostra uma tabela e pergunta qual deve ser
extraído.

A saída usa a mesma estrutura de `extract`, mas normalmente contém apenas um aluno:

- `report.json`: índice compacto da execução;
- `students/{student_id}/computer_monitoring.json`: eventos de ComputerMonitoring do aluno;
- `files/{student_id}/...`: imagens/evidências baixadas e ligadas ao JSON.

Também aceita:

```bash
smowl-analyser extract-student --download-workers 8
```

Esse modo não precisa que a lista de todos os alunos esteja carregada. Ele pode criar o registro
local diretamente a partir do JSON de ComputerMonitoring do aluno aberto.

### `resume`

Continua uma extração interrompida.

```bash
smowl-analyser resume --run-id "20260602-000000"
```

O `run_id` é o nome da pasta dentro de `data/runs/`. O comando lê `selection.json`,
`report.json` e `progress.json`, e tenta novamente alunos pendentes ou com falha.

Também aceita:

```bash
smowl-analyser resume --run-id "20260602-000000" --download-workers 8
```

## Análise Local

### `analyze`

Gera um relatório HTML local a partir de uma extração já feita.

```bash
smowl-analyser analyze --run-id "20260602-000000"
```

O comando lê os arquivos locais `students/*/computer_monitoring.json`. Ele não acessa Blackboard,
SMOW ou internet.

Saídas:

- `analysis/report.html`: relatório visual para revisão humana;
- `analysis/summary.json`: achados estruturados para uso futuro.

As regras iniciais sinalizam:

- ComputerMonitoring fechado e depois relançado;
- programas incomuns em relação ao restante da turma;
- títulos/sites incomuns em relação ao restante da turma;
- cópia/cola de múltiplas linhas, especialmente quando o texto parece código.

Isso é triagem para revisão humana, não conclusão disciplinar.

Opção útil:

```bash
smowl-analyser analyze --run-id "20260602-000000" --rarity-ratio 0.05
```

## Estrutura Dos Dados

Cada execução fica em:

```text
data/runs/{run_id}/
```

Arquivos principais:

- `report.json`: índice compacto da execução, com metadados e resumo dos alunos. Ele omite
  `students[].files` e `students[].smow.computer_monitoring`, porque esses detalhes ficam nos
  arquivos individuais;
- `manifest.json`: metadados da execução, URLs, versão do extrator, horários e contagens;
- `selection.json`: curso/atividade/relatório quando disponíveis;
- `progress.json`: alunos pendentes, concluídos e com falha;
- `students/{student_id}/computer_monitoring.json`: eventos de ComputerMonitoring do aluno;
- `files/{student_id}/...`: imagens/evidências baixadas.

Dados locais sensíveis:

- sessão: `.secrets/playwright-state.json`;
- extrações: `data/runs/{run_id}/`;
- diagnósticos: `data/diagnostics/{session_id}/`;
- aprendizado: `data/learning/{session_id}/`.

Esses diretórios devem permanecer fora do Git.

## Desenvolvimento E Diagnóstico

Os comandos abaixo são voltados para investigação, manutenção e melhoria do crawler. Eles devem
ficar para o fim do fluxo normal de uso.

### `inspect`

Inspeciona cursos, atividade SMOW, relatórios e alunos sem baixar evidências.

```bash
smowl-analyser inspect
```

Esse comando tenta navegar pelo Blackboard automaticamente. Use apenas para verificar se o crawler
consegue encontrar os elementos esperados. Para extração real, prefira `extract`.

### `diagnose`

Captura um pacote sanitizado de diagnóstico depois que você navega manualmente até a lista de
alunos do SMOW.

```bash
smowl-analyser diagnose
```

Salva em `data/diagnostics/{session_id}/`:

- `summary.json`: contagens de páginas, frames e endpoints SMOW;
- `pages.json` e `frames.json`: URLs, títulos e caminhos dos artefatos salvos;
- `network/responses.json`: metadados sanitizados das respostas;
- `network/json_responses.json`: payloads JSON sanitizados;
- `pages/*.html` e `frames/*.html`: HTML sanitizado.

Use quando `extract` não capturar dados internos do SMOW ou quando for necessário entender quais
endpoints a interface acionou.

### `learn`

Registra o caminho real feito no Blackboard/SMOW para ajudar a melhorar o crawler.

```bash
smowl-analyser learn
```

O comando pede checkpoints manuais, por exemplo:

1. Chegue na página da turma e pressione Enter.
2. Abra o SMOW e chegue na lista de alunos monitorados; depois pressione Enter.
3. Clique em um aluno ou em um ícone de detalhe/ação de um aluno e pressione Enter.
4. Abra detalhes/evidências de FrontCamera para esse aluno e pressione Enter.
5. Abra detalhes/evidências de ComputerMonitoring para esse aluno e pressione Enter.
6. Clique no botão de download do aluno ou de relatório individual e pressione Enter.
7. Abra uma evidência/imagem específica, se existir, e pressione Enter.

Salva em `data/learning/{session_id}/` HTML sanitizado, resumos de página, frames, rotas, deltas
de rede, respostas JSON/API e metadados de download. Não salva screenshots nem gera imagens.

## Testes

Para rodar os testes:

```bash
.venv/bin/python -m pytest
```
