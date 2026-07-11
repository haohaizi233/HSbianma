from hs_crawler.parser import (
    ParseError,
    SectionRow,
    parse_chapter_links,
    parse_detail_page,
    parse_search_page,
)


BASE_URL = "https://www.hsbianma.com"


def test_parse_chapter_links_returns_unique_two_digit_search_urls() -> None:
    html = """
    <div id="tree1">
      <a class="edit" href="/search?keywords=01">01</a>
      <a class="edit" href="/Search?keywords=02">02</a>
      <a class="edit" href="/search?keywords=01">duplicate</a>
      <a href="/search?keywords=03">not a chapter leaf</a>
      <a class="edit" href="/search?keywords=abc">invalid</a>
    </div>
    """

    assert parse_chapter_links(html, BASE_URL) == [
        "https://www.hsbianma.com/search?keywords=01",
        "https://www.hsbianma.com/Search?keywords=02",
    ]


def test_parse_search_page_extracts_detail_and_all_numeric_pagination_links() -> None:
    html = """
    <table class="result">
      <tr class="result-grid">
        <td><font color="red">01</font>01 1010.10</td>
        <td>item one</td>
        <td><a href="/Code/0101101010.html">details</a></td>
      </tr>
      <tr class="result-grid">
        <td>0101101090 <font color="gray">expired</font></td>
        <td>item two</td>
        <td><a href="https://www.hsbianma.com/Code/0101101090.html">details</a></td>
      </tr>
      <tr class="result-grid">
        <td>bad</td><td><a href="/Code/not-a-code.html">ignore</a></td>
      </tr>
    </table>
    <div id="pagination">
      <a href="/Search?keywords=01">1</a>
      <a href="/Search/2?keywords=01">2</a>
      <span class="pageindex">3</span>
      <a href="/Search/4?keywords=01">4</a>
      <a href="/Search/4?keywords=01">next</a>
    </div>
    """

    page = parse_search_page(html, BASE_URL)

    assert page.detail_urls == [
        "https://www.hsbianma.com/Code/0101101010.html",
        "https://www.hsbianma.com/Code/0101101090.html",
    ]
    assert page.pagination_urls == [
        "https://www.hsbianma.com/Search?keywords=01",
        "https://www.hsbianma.com/Search/2?keywords=01",
        "https://www.hsbianma.com/Search/4?keywords=01",
    ]


def test_parse_detail_page_preserves_all_section_rows_and_cleans_text() -> None:
    html = """
    <div id="code-info">
      <h3 class="ch3">基本信息</h3>
      <div class="cbox"><table>
        <tr><td class="td-label">商品编码</td><td class="td-txt">0101210010</td></tr>
        <tr><td class="td-label">商品名称</td><td class="td-txt"> 改良种用 <b>濒危野马</b> </td></tr>
        <tr><td class="td-label">商品描述</td><td class="td-txt"></td></tr>
        <tr><td class="td-label">编码状态</td><td class="td-txt">正常</td></tr>
      </table></div>
      <h3 class="ch3">申报要素</h3>
      <div class="cbox"><table>
        <tr><td class="td-label">0</td><td class="td-txt">品牌类型</td></tr>
        <tr><td class="td-label">1</td><td class="td-txt">出口享惠情况</td></tr>
      </table></div>
      <h3 class="ch3">监管条件</h3>
      <div class="cbox"><table>
        <tr><td class="td-label">A</td><td class="td-txt">入境货物通关单</td></tr>
        <tr><td class="td-label">F</td><td class="td-txt">濒危物种允许进口证明书</td></tr>
      </table></div>
      <h3 class="ch3">检验检疫类别</h3>
      <div class="cbox"><table><tr><td class="td-label">无</td></tr></table></div>
    </div>
    """

    record = parse_detail_page(html, f"{BASE_URL}/Code/0101210010.html")

    assert record.code == "0101210010"
    assert record.sections["基本信息"] == [
        SectionRow("商品编码", "0101210010"),
        SectionRow("商品名称", "改良种用 濒危野马"),
        SectionRow("商品描述", ""),
        SectionRow("编码状态", "正常"),
    ]
    assert record.sections["申报要素"] == [
        SectionRow("0", "品牌类型"),
        SectionRow("1", "出口享惠情况"),
    ]
    assert record.sections["监管条件"][1].value == "濒危物种允许进口证明书"
    assert record.sections["检验检疫类别"] == [SectionRow("无", "")]


def test_parse_detail_page_falls_back_to_code_in_source_url() -> None:
    html = """
    <div id="code-info">
      <h3 class="ch3">基本信息</h3>
      <div class="cbox"><table>
        <tr><td class="td-label">商品名称</td><td class="td-txt">test item</td></tr>
      </table></div>
    </div>
    """

    record = parse_detail_page(html, f"{BASE_URL}/Code/0101101010.html")

    assert record.code == "0101101010"


def test_parse_detail_page_rejects_missing_data_container() -> None:
    try:
        parse_detail_page("<html><body>not a detail page</body></html>", f"{BASE_URL}/Code/0101101010.html")
    except ParseError as exc:
        assert "#code-info" in str(exc)
    else:
        raise AssertionError("expected ParseError")


def test_parse_detail_page_extracts_same_origin_neighbor_links() -> None:
    html = """
    <div id="code-info">
      <h3 class="ch3">基本信息</h3>
      <div class="cbox"><table>
        <tr><td class="td-label">商品编码</td><td class="td-txt">0101101090</td></tr>
      </table></div>
    </div>
    <div class="cbox"><p>
      <span>上一条:<a href="/Code/0101101010.html">previous</a></span>
      <span>下一条:<a href="/Code/0101102010.html">next</a></span>
      <a href="https://other.example/Code/0101109999.html">off origin</a>
    </p></div>
    """

    record = parse_detail_page(html, f"{BASE_URL}/Code/0101101090.html")

    assert record.neighbor_urls == [
        f"{BASE_URL}/Code/0101101010.html",
        f"{BASE_URL}/Code/0101102010.html",
    ]
