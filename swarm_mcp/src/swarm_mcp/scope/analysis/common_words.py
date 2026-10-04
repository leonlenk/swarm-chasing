"""A small list of common English words (general, workplace and web vocabulary).

Used by ``recap.novel_terms`` to tell a coined term ("glimmerfax") from an ordinary word that
merely shows up late in a store. Hand-written, lowercase; inflections are matched by
``is_common`` (plural, -ed, -ing, -er, -ly), so only base forms are listed.
"""

from __future__ import annotations

COMMON_WORDS = frozenset(
    """
    a able about above accept access according account across act action active activity actual add address
    admin advance advice affect after again against age agency agent agree ahead aid aim air alert align all
    allow almost alone along already alright also alternative although always amazing among amount analysis
    analyze and angle announce annual another answer any anybody anyone anything anyway anywhere apart app
    apparent appear apply approach appropriate approve april archive area argue argument around arrange
    arrive art article artist ask aspect assess asset assign assist assume attach attempt attend attention
    audience audit august author auto automatic available average avoid await award aware away awesome
    back background backup bad balance ban bank bar base basic basis batch be bear beat beautiful because
    become bed before begin behind belief believe below benefit best better between beyond big bill bit
    black block blog blue board body book boost both bottom box brand break brief bright bring broad broken
    brown budget bug build bullet bunch business busy but button buy by
    calendar call calm camera campaign can cancel candidate cap capacity capital capture card care career
    careful carry case cash catch category cause caution cell center central certain chain chair challenge
    chance change channel chapter character charge charity chart chat cheap check child choice choose
    chunk circle city claim class classic clean clear click client climate clock close cloud club code
    coffee cold collaborate collect collection college color column combine come comfortable command
    comment commit common communicate community company compare competition complete complex component
    concern concept conclusion condition confirm conflict connect consider consistent constant contact
    contain content context continue contract contribute control conversation convert cool coordinate copy
    core corner correct cost could count country couple course cover create credit criteria critical cross
    current custom customer cut cycle
    daily damage dark dashboard data date day dead deadline deal dear death debate debug december decide
    decision deep default define definitely degree delay delete deliver demand demo depend deploy describe
    design desk detail determine develop device dialog did differ difference different difficult digital
    direct direction directly discover discuss discussion display distance distribute divide do doc
    document dog dollar domain door double doubt down download draft draw dream drive drop due duplicate
    during duty
    each early earn easy eat economy edge edit editor education effect effective effort either element else
    email empty enable end energy engage engine enjoy enough ensure enter entire entry environment equal
    error especially estimate even evening event ever every everybody everyone everything evidence exact
    example excellent except exchange excited exciting exist expand expect expensive experience experiment
    explain explore export express extend extra eye
    face fact factor fail fair fall false familiar family famous fan far fast feature february fee feed
    feedback feel few field figure file fill final finally financial find fine finish fire firm first fit
    five fix flag flat flow focus folder follow food for force forget form format forward four frame free
    fresh friday friend from front full fun function fund fundraiser future
    gain game gap gather general generate get gift give glad global goal good google grab grant graph great
    green grid ground group grow guess guest guide guy
    half hand handle hang happen happy hard head health hear heart heavy hello help helpful here hey hi high
    highlight history hit hold holiday home hope host hot hour house however huge human hundred
    idea identify if image impact implement important improve include income increase indeed independent
    index individual info inform information initial input inside insight instance instead interest
    interesting internal internet into intro introduce invite involve issue it item
    january job join journal july jump june just
    keep key kind know knowledge
    label lack land language large last late later launch layer layout lead learn least leave left legal
    less lesson let letter level library life light like likely limit line link list listen little live
    load local location lock log logic long look loop lose lot love low
    machine main maintain major make manage manager many map march mark market match material matter maybe
    mean measure media medium meet meeting meetup member memory mention menu message method middle might
    mind minor minute miss mission mistake mode model modern moment monday money month more morning most
    move much multiple music must
    name natural near nearly necessary need negative network never new news newsletter next nice night no
    node none normal note nothing notice november now number
    object observe obvious occur october odd of off offer office official often okay old on once one online
    only open operation opinion option or order organization organize original other otherwise our out
    outcome outline output outside over overall overview own owner
    pace page pair panel paper parent part participate particular partner party pass past path pattern
    pause pay people per percent perfect perform perhaps period permission person personal phase phone
    pick picture piece pilot pin place plan platform play player please plenty plus point policy poll
    poor popular position positive possible post potential power practice prepare present press pretty
    prevent preview price primary print prior priority private probably problem process produce product
    profile program progress project promise proof proper property propose protect provide public
    publish pull purpose push put puzzle
    quality question quick quiet quite quote
    raise random range rate rather reach read reader reading ready real really reason receive recent
    recommend record red reduce refer reference reflect region regular related release relevant remain
    remember remind remote remove repeat replace reply report request require research resource respond
    response rest result resume return review right risk road role room root round rule run
    safe same sample save say scale schedule school science score screen search season second section
    secure see seem select self sell send sense separate september series serious serve server service
    session set setting setup several shape share sheet shift ship short should show side sign signal
    signup similar simple since single site situation size skill skip slide slow small smart so social
    some someone something sometimes soon sorry sort sound source space speak special specific speed spend
    split spot spreadsheet staff stage stand standard start state statement status stay step still stop
    store story strategy stream street strong structure stuff style subject submit success such suggest
    summary sunday support sure survey switch system
    table tag take talk target task team tech technical template term test text than thank that the theme
    then theory there thing think third this though thought thread three through thursday ticket tidy time
    tip title to today together tomorrow too tool top topic total touch toward track tracker trade train
    transfer tree trend trial trip true trust try tuesday turn twice two type typical
    under understand unique unit until up update upload upon usage use useful user usual
    valid value various verify version very video view visit voice volunteer vote
    wait walk want warm watch water way web website wednesday week weekly weight welcome well what whatever
    when where whether which while white who whole why wide will win window wish with within without word
    work world worry worth would write wrong
    year yes yesterday yet you young
    zero zone
    faq ok hmm yeah wow cheers kudos asap etc
    """.split()
)


def is_common(word: str) -> bool:
    """Whether ``word`` (lowercase) is a listed word or a regular inflection of one; a hyphenated
    word is common when every part is ("fact-check")."""
    if "-" in word:
        parts = [p for p in word.split("-") if p]
        return bool(parts) and all(is_common(p) for p in parts)
    if word in COMMON_WORDS:
        return True
    for suffix, repl in (
        ("ies", "y"),
        ("es", ""),
        ("s", ""),
        ("ied", "y"),
        ("ed", ""),
        ("ed", "e"),
        ("d", ""),
        ("ing", ""),
        ("ing", "e"),
        ("ers", ""),
        ("er", ""),
        ("er", "e"),
        ("ly", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            stem = word[: len(word) - len(suffix)] + repl
            if stem in COMMON_WORDS:
                return True
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[:-1] in COMMON_WORDS:  # planned, running
                return True
    return False
