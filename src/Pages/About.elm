module Pages.About exposing (view)

import Html exposing (Html, div, h1, h2, p, img, section, text)
import Html.Attributes exposing (class, style, src, alt)

view : Html msg
view =
    div [ class "about-page" ]
        [ -- Banner Section
          section [ class "about-banner" ]
            [ img [ src "https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=1200&q=80"
                  , alt "About Us Banner"
                  , class "about-banner-img"
                  ] []
            , div [ class "about-banner-content" ]
                [ h1 [ class "about-title" ] [ text "About Us" ]
                , p [ class "about-subtitle" ] [ text "Empowering your financial journey with modern, transparent, and secure digital solutions." ]
                ]
            ]
        , section [ class "about-section" ]
            [ h2 [] [ text "Who We Are" ]
            , p [] [ text "We are a passionate team of finance and technology professionals dedicated to making financial tools accessible, easy, and secure for everyone. Our platform offers a suite of products including loan calculators, product comparisons, and educational resources to help you make informed decisions." ]
            , h2 [] [ text "Our Mission" ]
            , p [] [ text "To empower individuals and businesses to take control of their financial future through innovative, user-friendly, and transparent digital solutions." ]
            , h2 [] [ text "Why Choose Us?" ]
            , p [] [ text "We combine cutting-edge technology with deep financial expertise to deliver tools that are not only powerful but also easy to use. Your privacy and security are our top priorities, and we never store your personal financial data." ]
            ]
        ] 